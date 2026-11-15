/*
 * wireguard.exe port for Wine/Proton.
 *
 * Why this exists
 * ---------------
 * WireGuard for Windows creates its tunnel through WireGuardNT: a kernel-mode
 * NDIS driver (wireguard.sys) that wireguard.dll installs and loads at runtime.
 * Wine cannot load kernel drivers, so `wireguard.exe /installtunnelservice`
 * panics in WireGuardCreateAdapter and no adapter ever appears. See
 * research/settlers-united-wireguard-wine-2026-11-15.md for the evidence.
 *
 * This program keeps the *documented* wireguard-windows command line that
 * applications drive (Settlers United uses exactly these three verbs) but
 * performs the tunnel on the Linux side, where WireGuard does work: it asks a
 * small root helper to run `wg-quick up|down <conf>`. The helper is the
 * authority on whether that worked -- it verifies the interface in the same
 * network namespace it ran in, and reports back through a status file (Wine
 * caches its interface list per process, so polling GetAdaptersAddresses from
 * here cannot be trusted). The port then registers the WireGuardTunnel$<name>
 * service so service-state based checks (`sc queryex`) keep working.
 *
 * The helper is invoked through `start.exe /unix`, Wine's supported way for a
 * PE to run a Unix program. Its path comes from %WINE_WG_HELPER% and defaults to
 * /usr/local/bin/wine-wg (the wrapper that escalates through pkexec).
 *
 * Success is never assumed: a tunnel that did not come up, a service that could
 * not be registered, and a teardown that did not happen all return non-zero, so
 * the calling application shows its own error instead of running without a VPN.
 *
 * Build: see port/build.sh (nix, mingw-w64 cross).
 */

#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winsvc.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>
#include <wctype.h>

#define TUNNEL_SERVICE_PREFIX L"WireGuardTunnel$"
#define DEFAULT_HELPER "/usr/local/bin/wine-wg"
#define DEFAULT_STATUS_DIR "/run/wine-wg"
#define HELPER_TIMEOUT_MS 90000
#define SERVICE_STOP_TIMEOUT_MS 15000
#define SERVICE_HELPER_TIMEOUT_MS 15000
#define NAME_MAX_LEN 32

static SERVICE_STATUS_HANDLE status_handle;
static char helper_command[1024];      /* unix path of the wg helper */
static DWORD helper_timeout = HELPER_TIMEOUT_MS;
static SERVICE_STATUS service_status;
static HANDLE stop_event;

static void log_msg( const char *fmt, ... )
{
    va_list args;

    va_start( args, fmt );
    vfprintf( stderr, fmt, args );
    fputc( '\n', stderr );
    fflush( stderr );
    va_end( args );
}

/* Tunnel names follow wireguard-windows' conf.NameFromPath(): the base name of
 * the config file without ".conf". wg-quick additionally limits an interface
 * name to 15 characters, so a longer name is refused here instead of silently
 * creating a differently named interface. */
static BOOL conf_name_from_path( const WCHAR *path, WCHAR *name, size_t len )
{
    const WCHAR *base = path, *p;
    size_t n;

    for (p = path; *p; p++) if (*p == '\\' || *p == '/') base = p + 1;
    n = wcslen( base );
    if (n > 5 && !wcsicmp( base + n - 5, L".conf" )) n -= 5;
    if (!n || n > NAME_MAX_LEN || n > 15 || n >= len) return FALSE;
    /* wg-quick hands the name to ip/wg, where a leading '-' would be an option */
    if (base[0] == '-' || base[0] == '.') return FALSE;
    for (p = base; p < base + n; p++)
        if (!iswalnum( *p ) && *p != '_' && *p != '=' && *p != '+' && *p != '.' && *p != '-')
            return FALSE;

    memcpy( name, base, n * sizeof(WCHAR) );
    name[n] = 0;
    return TRUE;
}

/* Wine-only export: the Unix path of a DOS path. */
static char *unix_path( const WCHAR *path )
{
    static char * (CDECL *get_unix)( const WCHAR * );

    if (!get_unix && !(get_unix = (void *)GetProcAddress( GetModuleHandleW( L"kernel32.dll" ),
                                                          "wine_get_unix_file_name" )))
        return NULL;
    return get_unix( path );
}

/* The host helper runs detached (`start.exe /unix` cannot report an exit
 * code), so it leaves its outcome in a status file that we poll. The file lives
 * in a root-owned directory (never in a user-writable one, which would make the
 * helper a root-write primitive), and its name carries a nonce so a stale
 * status from an earlier run can never be mistaken for this one. */
static void status_dir( WCHAR *dir, size_t len )
{
    char ansi[MAX_PATH];

    if (!GetEnvironmentVariableA( "WINE_WG_STATUS_DIR", ansi, sizeof(ansi) ))
        strcpy( ansi, DEFAULT_STATUS_DIR );
    MultiByteToWideChar( CP_UTF8, 0, ansi, -1, dir, len );
    dir[len - 1] = 0;
}

static void status_name( WCHAR *out, size_t len )
{
    swprintf( out, len, L"wine-wg-%lu-%lu.status", GetCurrentProcessId(), GetTickCount() );
}

/* Wine resolves the \\?\unix prefix to a host path, so the helper's file needs
 * no drive mapping. */
static HANDLE open_status( const WCHAR *dir, const WCHAR *name, DWORD access )
{
    WCHAR path[MAX_PATH];

    swprintf( path, ARRAYSIZE(path), L"\\\\?\\unix%ls/%ls", dir, name );
    return CreateFileW( path, access, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                        OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL );
}

/* 1 = ok, 0 = the helper reported a failure, -1 = no status file (yet). */
static int read_status( const WCHAR *dir, const WCHAR *name, char *error, size_t error_len )
{
    char buffer[512];
    HANDLE file;
    DWORD read = 0;

    if (error_len) error[0] = 0;
    if ((file = open_status( dir, name, GENERIC_READ )) == INVALID_HANDLE_VALUE) return -1;
    buffer[0] = 0;
    if (!ReadFile( file, buffer, sizeof(buffer) - 1, &read, NULL )) read = 0;
    CloseHandle( file );
    buffer[read] = 0;
    buffer[strcspn( buffer, "\r\n" )] = 0;
    if (!strcmp( buffer, "ok" )) return 1;
    if (error_len) snprintf( error, error_len, "%s", buffer[0] ? buffer : "(empty status)" );
    return 0;
}

static int wait_status( const WCHAR *dir, const WCHAR *name, char *error, size_t error_len )
{
    DWORD waited;
    int result;

    for (waited = 0; waited < helper_timeout; waited += 250)
    {
        if ((result = read_status( dir, name, error, error_len )) >= 0) return result;
        Sleep( 250 );
    }
    return -1;
}

static void resolve_helper( void )
{
    if (!helper_command[0] && !GetEnvironmentVariableA( "WINE_WG_HELPER", helper_command, sizeof(helper_command) ))
        strcpy( helper_command, DEFAULT_HELPER );
}

static void helper_path( WCHAR *helper, size_t len )
{
    resolve_helper();
    MultiByteToWideChar( CP_UTF8, 0, helper_command, -1, helper, len );
    helper[len - 1] = 0;
}

/* Ask the host to run wg-quick. `start.exe /unix` is Wine's supported bridge
 * from a PE to a Unix program; it detaches, so the reported outcome comes from
 * the helper's status file, not from an exit code. */
static BOOL run_helper( const char *action, const char *name, const WCHAR *conf )
{
    WCHAR cmd[4096], helper[1024], dir[MAX_PATH], status[MAX_PATH], unix_conf_w[4096];
    STARTUPINFOW si = { .cb = sizeof(si) };
    PROCESS_INFORMATION pi;
    char *unix_conf = NULL, error[512] = "";
    int result, len;

    helper_path( helper, ARRAYSIZE(helper) );
    status_dir( dir, ARRAYSIZE(dir) );
    status_name( status, ARRAYSIZE(status) );

    if (conf)
    {
        if (!(unix_conf = unix_path( conf )))
        {
            log_msg( "could not convert %ls to a unix path", conf );
            return FALSE;
        }
        MultiByteToWideChar( CP_UTF8, 0, unix_conf, -1, unix_conf_w, ARRAYSIZE(unix_conf_w) );
        len = swprintf( cmd, ARRAYSIZE(cmd), L"start.exe /unix \"%ls\" %hs \"%ls\" %hs \"%ls\"",
                        helper, action, unix_conf_w, name, status );
    }
    else
        len = swprintf( cmd, ARRAYSIZE(cmd), L"start.exe /unix \"%ls\" %hs %hs \"%ls\"",
                        helper, action, name, status );
    if (len < 0 || len >= ARRAYSIZE(cmd))
    {
        log_msg( "the wg helper command line does not fit (%d wide characters)", len );
        return FALSE;
    }
    if (getenv( "WINE_WG_DEBUG" )) fwprintf( stderr, L"wine-wg: running %ls\n", cmd );

    if (!CreateProcessW( NULL, cmd, NULL, NULL, FALSE, 0, NULL, NULL, &si, &pi ))
    {
        log_msg( "could not run the wg helper: error %lu", GetLastError() );
        return FALSE;
    }
    WaitForSingleObject( pi.hProcess, helper_timeout );
    CloseHandle( pi.hProcess );
    CloseHandle( pi.hThread );

    result = wait_status( dir, status, error, sizeof(error) );
    open_status( dir, status, DELETE );  /* best effort cleanup; the rename in the helper already replaced it */
    if (result < 0)
    {
        log_msg( "the wg helper did not report back (see journalctl -t wine-wg)" );
        return FALSE;
    }
    if (result == 0)
    {
        log_msg( "%s", error );
        return FALSE;
    }
    return TRUE;
}

static WCHAR *service_name( const WCHAR *name )
{
    static WCHAR buffer[128];

    swprintf( buffer, ARRAYSIZE(buffer), TUNNEL_SERVICE_PREFIX L"%ls", name );
    return buffer;
}

static BOOL create_service( const WCHAR *name, const WCHAR *conf )
{
    static const WCHAR deps[] = L"Nsi\0TcpIp\0";
    WCHAR exe[MAX_PATH], binpath[2 * MAX_PATH], display[256];
    SC_HANDLE manager, service;
    BOOL ok = FALSE;

    if (!GetModuleFileNameW( NULL, exe, MAX_PATH )) return FALSE;
    {
        WCHAR helper_w[1024];
        helper_path( helper_w, ARRAYSIZE(helper_w) );
        swprintf( binpath, ARRAYSIZE(binpath), L"\"%ls\" /tunnelservice \"%ls\" \"%ls\"",
                  exe, conf, helper_w );
    }
    swprintf( display, ARRAYSIZE(display), L"WireGuard Tunnel: %ls", name );

    if (!(manager = OpenSCManagerW( NULL, NULL, SC_MANAGER_CREATE_SERVICE ))) return FALSE;
    service = CreateServiceW( manager, service_name( name ), display, SERVICE_ALL_ACCESS,
                              SERVICE_WIN32_OWN_PROCESS, SERVICE_DEMAND_START, SERVICE_ERROR_NORMAL,
                              binpath, NULL, NULL, deps, L"LocalSystem", NULL );
    if (service) ok = TRUE;
    else if (GetLastError() == ERROR_SERVICE_EXISTS)
    {
        service = OpenServiceW( manager, service_name( name ), SERVICE_CHANGE_CONFIG );
        if (service) ok = ChangeServiceConfigW( service, SERVICE_NO_CHANGE, SERVICE_NO_CHANGE,
                                                SERVICE_NO_CHANGE, binpath, NULL, NULL, NULL, NULL, NULL, NULL );
    }

    if (service) CloseServiceHandle( service );
    CloseServiceHandle( manager );
    return ok;
}

static void delete_service( const WCHAR *name )
{
    SC_HANDLE manager, service;
    SERVICE_STATUS status;
    DWORD waited, attempt;

    if (!(manager = OpenSCManagerW( NULL, NULL, SC_MANAGER_CONNECT )))
    {
        log_msg( "could not open the service manager: error %lu", GetLastError() );
        return;
    }
    service = OpenServiceW( manager, service_name( name ), SERVICE_STOP | SERVICE_QUERY_STATUS | DELETE );
    if (!service)
    {
        log_msg( "service %ls is not registered: error %lu", service_name( name ), GetLastError() );
        CloseServiceHandle( manager );
        return;
    }

    if (!ControlService( service, SERVICE_CONTROL_STOP, &status ) && GetLastError() != ERROR_SERVICE_NOT_ACTIVE)
        log_msg( "could not stop %ls: error %lu", service_name( name ), GetLastError() );
    for (waited = 0; waited < SERVICE_STOP_TIMEOUT_MS; waited += 100)
    {
        if (!QueryServiceStatus( service, &status )) break;
        if (status.dwCurrentState == SERVICE_STOPPED) break;
        Sleep( 100 );
    }

    /* A service stops existing only once the controller has reaped the process,
     * so retry rather than reporting a teardown that did not happen. */
    for (attempt = 0; attempt < 50; attempt++)
    {
        if (DeleteService( service )) break;
        if (GetLastError() != ERROR_SERVICE_MARKED_FOR_DELETE && attempt == 49)
            log_msg( "could not delete %ls: error %lu", service_name( name ), GetLastError() );
        Sleep( 100 );
    }
    CloseServiceHandle( service );

    /* Report a settled state: the entry disappears only once the controller has
     * reaped the tunnel process, which is asynchronous. */
    for (attempt = 0; attempt < 50; attempt++)
    {
        service = OpenServiceW( manager, service_name( name ), SERVICE_QUERY_STATUS );
        if (!service) break;
        CloseServiceHandle( service );
        Sleep( 100 );
    }
    CloseServiceHandle( manager );
}

/* Starts the tunnel service and treats "already running" as success, so that a
 * reinstall is idempotent without logging a spurious error. */
static BOOL start_service( const WCHAR *name )
{
    SC_HANDLE manager, service;
    BOOL ok = FALSE;

    if (!(manager = OpenSCManagerW( NULL, NULL, SC_MANAGER_CONNECT ))) return FALSE;
    if ((service = OpenServiceW( manager, service_name( name ), SERVICE_START )))
    {
        if (StartServiceW( service, 0, NULL )) ok = TRUE;
        else if (GetLastError() == ERROR_SERVICE_ALREADY_RUNNING) ok = TRUE;
        else log_msg( "could not start %ls: error %lu", service_name( name ), GetLastError() );
        CloseServiceHandle( service );
    }
    else log_msg( "could not open %ls: error %lu", service_name( name ), GetLastError() );
    CloseServiceHandle( manager );
    return ok;
}

static int install_tunnel( const WCHAR *conf )
{
    WCHAR name[NAME_MAX_LEN + 1];
    char name_a[NAME_MAX_LEN + 1];

    if (!conf_name_from_path( conf, name, ARRAYSIZE(name) ))
    {
        log_msg( "%ls: not a usable tunnel name", conf );
        return 1;
    }
    WideCharToMultiByte( CP_UTF8, 0, name, -1, name_a, sizeof(name_a), NULL, NULL );

    if (GetFileAttributesW( conf ) == INVALID_FILE_ATTRIBUTES)
    {
        log_msg( "cannot read %ls: error %lu", conf, GetLastError() );
        return 1;
    }
    if (!run_helper( "up", name_a, conf )) return 1;

    /* The application's only observable is the service entry, so a tunnel that
     * is up but unregistered is a failure, not a warning. */
    if (!create_service( name, conf ))
    {
        log_msg( "the tunnel is up but %ls could not be registered: error %lu",
                 service_name( name ), GetLastError() );
        return 1;
    }
    if (!start_service( name )) return 1;
    return 0;
}

/* A teardown that did not happen is reported as such and the service entry is
 * kept: deleting it would tell the application the tunnel is gone while the
 * interface is still there. */
static int uninstall_tunnel( const WCHAR *name )
{
    char name_a[NAME_MAX_LEN + 1];

    WideCharToMultiByte( CP_UTF8, 0, name, -1, name_a, sizeof(name_a), NULL, NULL );
    if (!run_helper( "down", name_a, NULL ))
    {
        log_msg( "the tunnel is still up, keeping %ls registered", service_name( name ) );
        return 1;
    }
    delete_service( name );
    return 0;
}

static DWORD WINAPI service_handler( DWORD control, DWORD event_type, void *event_data, void *context )
{
    switch (control)
    {
    case SERVICE_CONTROL_STOP:
    case SERVICE_CONTROL_SHUTDOWN:
        service_status.dwCurrentState = SERVICE_STOP_PENDING;
        SetServiceStatus( status_handle, &service_status );
        if (stop_event) SetEvent( stop_event );
        return NO_ERROR;
    case SERVICE_CONTROL_INTERROGATE:
        return NO_ERROR;
    }
    return ERROR_CALL_NOT_IMPLEMENTED;
}

/* The service controller only hands ServiceMain the *service name*; the
 * arguments live on the process command line (this is also how
 * wireguard-windows finds its config), so remember them in wmain. */
static WCHAR service_conf[MAX_PATH];

static void WINAPI service_main( DWORD argc, WCHAR *argv[] )
{
    WCHAR name[NAME_MAX_LEN + 1], conf[MAX_PATH];
    char name_a[NAME_MAX_LEN + 1];

    status_handle = RegisterServiceCtrlHandlerExW( L"", service_handler, NULL );
    if (!status_handle) return;


    service_status.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    service_status.dwCurrentState = SERVICE_START_PENDING;
    service_status.dwControlsAccepted = 0;
    SetServiceStatus( status_handle, &service_status );

    wcsncpy( conf, service_conf, ARRAYSIZE(conf) - 1 );
    conf[ARRAYSIZE(conf) - 1] = 0;
    if (!conf[0] || !conf_name_from_path( conf, name, ARRAYSIZE(name) ))
    {
        log_msg( "the service was started without a usable tunnel config" );
        service_status.dwCurrentState = SERVICE_STOPPED;
        service_status.dwWin32ExitCode = ERROR_SERVICE_SPECIFIC_ERROR;
        service_status.dwServiceSpecificExitCode = 1;
        SetServiceStatus( status_handle, &service_status );
        return;
    }
    WideCharToMultiByte( CP_UTF8, 0, name, -1, name_a, sizeof(name_a), NULL, NULL );

    stop_event = CreateEventW( NULL, TRUE, FALSE, NULL );
    service_status.dwCurrentState = SERVICE_RUNNING;
    service_status.dwControlsAccepted = SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN;
    SetServiceStatus( status_handle, &service_status );

    /* The tunnel itself lives in the host kernel; this process only owns its
     * lifetime, exactly like wireguard.exe's own /tunnelservice. */
    WaitForSingleObject( stop_event, INFINITE );
    run_helper( "down", name_a, NULL );

    service_status.dwCurrentState = SERVICE_STOPPED;
    service_status.dwControlsAccepted = 0;
    SetServiceStatus( status_handle, &service_status );
}

static void usage( void )
{
    fprintf( stderr,
             "Usage: wireguard.exe /installtunnelservice CONFIG_PATH\n"
             "       wireguard.exe /uninstalltunnelservice TUNNEL_NAME\n"
             "       wireguard.exe /tunnelservice CONFIG_PATH\n" );
}

int __cdecl wmain( int argc, WCHAR *argv[] )
{
    if (argc < 2) { usage(); return 1; }

    if (!wcscmp( argv[1], L"/version" ))
    {
        printf( "wireguard.exe (Wine port) 0.1\n" );
        return 0;
    }
    if (argc < 3) { usage(); return 1; }

    if (!wcscmp( argv[1], L"/installtunnelservice" )) return install_tunnel( argv[2] );
    if (!wcscmp( argv[1], L"/uninstalltunnelservice" )) return uninstall_tunnel( argv[2] );
    if (!wcscmp( argv[1], L"/tunnelservice" ))
    {
        SERVICE_TABLE_ENTRYW table[] = { { (WCHAR *)L"", service_main }, { NULL, NULL } };

        wcsncpy( service_conf, argv[2], ARRAYSIZE(service_conf) - 1 );
        /* The service runs from the controller's environment, where our
         * variables do not exist, so the helper is passed on the command line. */
        helper_timeout = SERVICE_HELPER_TIMEOUT_MS;
        if (argc >= 4) WideCharToMultiByte( CP_UTF8, 0, argv[3], -1, helper_command, sizeof(helper_command), NULL, NULL );
        if (!StartServiceCtrlDispatcherW( table )) log_msg( "not started by the service controller" );
        return 0;
    }

    usage();
    return 1;
}
