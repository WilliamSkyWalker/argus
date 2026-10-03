# Chrome Web Store submission — Saygo Browser 0.4.11

Status: package uploaded by publisher; store review not submitted. Isolated
Chromium/native-host validation passed for input, independently targeted left/right
scrolling, screenshots and release. Screenshots use a fictional local test page.
Store ID: ehomcchjfomfkcmbeinlcmpbaamdhfbo. Runtime minimum for targeted scrolling:
Saygo 0.4.11, native bridge protocol 1. Extension and runtime versions are independent.

## Store listing

Name: Saygo Browser
Category: Developer Tools (or the closest available tools category)
Language: English
Homepage: https://saygo.work/
Support: https://github.com/WilliamSkyWalker/saygo/issues
Privacy: https://saygo.work/privacy.html (verify it is deployed before submitting)
Video: leave blank unless a public demonstration video is available.

### Description (paste into the dashboard)

Saygo Browser connects your existing browser tabs to a local Saygo installation so your AI coding assistant can help you operate and debug websites.

Use it to inspect page screenshots, click controls, enter text, drag, navigate between pages, and scroll a specific area of a page. It also provides network observations for debugging, including HTTP requests and responses, WebSocket messages, and server-sent events.

You stay in control:
- Click Connect local bridge to start a session.
- All supported website tabs in the connected browser profile, including newly opened tabs and popups, become available to your local Agent.
- Click Release browser and disconnect to stop control and clear the extension's network journal.
- Existing website login sessions remain in your browser.

Setup required:
This extension requires the separately installed Saygo application and a compatible AI client. Install saygo-agent-control from PyPI using pipx, then run saygo setup for your client and configure the native bridge for this extension's ID. Setup instructions are at https://saygo.work/ and in the project documentation. The extension does not include an AI model or subscription.

Privacy:
While connected, Saygo can access page URLs, titles, screenshots, form input and network traffic. Network recording starts automatically for supported tabs and can include sensitive page content, cookies and authentication tokens. Data is sent to your local Saygo runtime; your AI client may send that data to its configured model provider. Saygo has no developer analytics endpoint and does not sell this data. Local task records can remain after disconnecting. Connect only a browser profile you intend to share with your Agent.

Saygo Browser is open source under the MIT license. Browser-restricted pages such as chrome:// pages are not supported. See the privacy policy and documentation for details.

## Privacy practices

Single purpose:
Enable user-directed browser operation and debugging by connecting website tabs to the user's locally installed Saygo Agent runtime.

Permission justifications:

- debugger: Required to capture viewport screenshots, dispatch pointer/keyboard/wheel input, and observe HTTP/WebSocket/SSE traffic for the connected Agent. Saygo uses a fixed allowlist of CDP operations; it does not expose arbitrary page-script execution. User connection is required and Release detaches the debugger.
- tabs: Required to enumerate supported HTTP(S) tabs, read their URLs/titles, and explicitly select, create, navigate or close the tab requested by the user. New tabs and popups are surfaced for explicit selection.
- storage: Stores session-scoped browser identity and released-tab state so cancelled control is respected and stale tab IDs cannot silently target a new tab. Not used for advertising profiles.
- nativeMessaging: Exchanges bounded JSON commands and observations with the separately installed com.saygo.browser host on the same computer. The extension does not open a remote control server.

Remote code: No. Extension JavaScript is bundled in the ZIP. Native messages contain
structured commands, not downloaded JavaScript. The native host is separate local
software installed by the user; disclose that dependency to reviewers.

Data categories: Declare website content, web history (URLs/titles), and user activity.
Because connected screenshots and traffic may include them, review and accurately
include personally identifiable information, authentication information, personal
communications, financial/payment information, health information and location.
Do not claim no data handling merely because the first recipient is local.
No advertising, sale of data, or credit/lending use. Check certifications only when
consistent with the publisher's actual practices and the published privacy policy.

## Test instructions

No Saygo account, login credentials or payment are required. Please use a clean
browser profile with test pages, not a profile containing private accounts.

1. Install Python 3.10+, pipx and a supported Agent CLI (Codex, Claude Code, Qoder or QoderCN). Install the extension from the review package.
2. Run: pipx install 'saygo-agent-control[mcp]>=0.4.11'
3. Run: saygo setup --client codex --extension-id ehomcchjfomfkcmbeinlcmpbaamdhfbo
   Use --client claude/qoder/qodercn if appropriate. On WSL, run setup in WSL; it prepares the native host on Windows. On macOS/Linux, run setup on the browser's host OS. Do not load the development extension when testing the store ID.
4. Open https://example.com/ in the test browser, open Saygo Browser, and click Connect local bridge. Verify Connected and the test tab title in the popup. Chrome may display a debugger indicator.
5. To test without an Agent conversation, connect and list available pages: saygo device connect --platform browser --session store-review --backend extension
6. Select the returned test page ID: saygo device select-page PAGE_ID --session store-review
7. Run: saygo device screenshot --session store-review --out /tmp/saygo-review.png
   On Windows use a writable Windows file path instead of /tmp.
8. For targeted scrolling, open a scrollable test page, observe it, then run: saygo device act '{"type":"scroll_at","x":50,"y":60,"coordinate_space":"percent","amount":-3}' --session store-review --observe-after
   The scroll point is in viewport percentages; positive amounts scroll up.
9. Click Release browser and disconnect. The popup should show Disconnected and subsequent control requests must fail until the user reconnects. No hidden background connection is created.

If the native host is missing, setup is required; this is an expected external
application dependency rather than a website login error. Please contact support
if test setup fails rather than testing the extension without its native host.

## Remaining submission checks

- Confirm store-assigned extension ID and public contact information.
- Verify the public privacy URL returns the policy, not a generic site fallback.
- Upload the 128x128 icon, a real 1280x800 or 640x400 screenshot, and 440x280 promo tile.
- Complete privacy and distribution fields, save draft, then submit for review.
- After approval, validate actual store installation, update, and removal separately.

Official references:
https://developer.chrome.com/docs/webstore/publish
https://developer.chrome.com/docs/webstore/cws-dashboard-privacy
https://developer.chrome.com/docs/webstore/images
