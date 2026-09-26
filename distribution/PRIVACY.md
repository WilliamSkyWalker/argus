# Argus Browser privacy policy

Argus Browser is a local bridge for user-directed programming Agents. Connecting
it makes website tabs in the current browser profile available to the local Argus
runtime, including tabs opened later. You can disconnect with Release in the popup.

While connected, the extension can read tab URLs and titles, capture screenshots,
and send visual input. It automatically observes network traffic for controllable
tabs. The bounded in-memory network journal may contain request/response headers,
bodies, WebSocket messages and server-sent events. These can contain personal data,
cookies, passwords, access tokens or other sensitive page content. Browser-restricted
pages cannot be controlled. Do not connect a profile containing data you do not
want your Agent to access.

The extension communicates with a separately installed native messaging host on
your computer. It does not send developer analytics or sell data. The local Argus
runtime may save observations, action records and reports on disk. Your Agent and
any model service it uses may receive this information; their settings and privacy
policies govern that processing. This policy does not promise that your Agent
keeps all information on your computer.

Disconnecting ends extension control and clears the active network journal. It does
not erase records already returned to the Agent or saved by Argus. Removing the
extension or unregistering the native host also preserves local task records. You
can delete those records from your Argus data directory when no longer needed.

Questions: https://github.com/WilliamSkyWalker/argus/issues (do not include secrets
or private screenshots in public issues).
