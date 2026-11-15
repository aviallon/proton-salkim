# wireguard.exe port

The official WireGuard for Windows installs `wireguard.sys`, a kernel-mode NDIS
driver, so it cannot create a tunnel under Wine. `wireguard.exe` here keeps the
vendor's documented command line (`/installtunnelservice`,
`/uninstalltunnelservice`, `/tunnelservice`) but asks the host to run
`wg-quick` instead, then registers the `WireGuardTunnel$<name>` service so
applications that check for it with `sc queryex` keep working.

It is used by Settlers United, whose login flow brings a tunnel up and greps
`sc queryex` for its own service. The host side (a validating helper invoked
through pkexec) has to be installed separately, because creating an interface
takes root.

`wireguard.c` is the source; it is built with

    x86_64-w64-mingw32-gcc -O2 -static -municode -o wireguard.exe wireguard.c -lws2_32

and is copied into the prefix by a protonfixes local fix (see the project that
ships this build), because a fix can only be keyed by game id.
