# Maply deployment

The authoritative app, database and image storage are on the permanent server Mac:
- SSH: `ssh maply-server` (harlenairbook@192.168.0.249; update the local SSH alias if its DHCP address changes).
- Project: `/Users/harlenairbook/Maply`.
- Data: `/Users/harlenairbook/Maply/data`.
- Runtime: Colima + Docker Compose. Remote commands need `PATH=/opt/homebrew/bin:/usr/local/bin:$PATH`.

This MacBook Pro checkout is the development/client workspace. `data/client-server.json`
identifies that role. Local `http://127.0.0.1:8000` is an SSH tunnel to the server,
maintained by the `io.maply.client-tunnel` user LaunchAgent. Do not start the local
app/database for normal use or treat its old data as current progress. The local
Docker volume and transfer bundle are retained as migration backups.

A server checkout has `data/server-transfer-receipt.json`. On the server, normal
Docker Compose operations are appropriate; `io.maply.server` starts the app and
prevents idle sleep after login. Native Chrome capture runs in the server user's
Terminal with macOS permissions, independently of the containerized app.

When implementing changes here, deploy the relevant source files to the server
and rebuild there if necessary. Preserve the server's `.env`, `data/`, database
volume and SSH setup. Never sync this checkout's old data over the server. Keep
credentials in their existing private files; never commit transfer bundles or keys.
Database tests must target an isolated test database, never the server's `maply` database.
