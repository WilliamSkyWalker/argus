# Chrome Web Store submission draft

Name: Argus Browser

Summary: Connect browser tabs to your local Argus Agent for visual operation and debugging.

Description:
Argus Browser connects your existing website tabs to a locally installed Argus
runtime. With your permission, your programming Agent can inspect screenshots,
click, type, scroll and observe network traffic while working on browser tasks.
Click Connect to share website tabs in the current profile, including new tabs and
popups. Click Release to disconnect and stop Argus control. A separate local
Argus installation is required; the extension alone does not provide an AI model.

Single purpose: Enable user-directed local Agent operation and debugging of website tabs.

Permission explanations:
- debugger: screenshots, visual input and passive HTTP/WebSocket/SSE observation via CDP.
- tabs: enumerate controllable tabs and identify the selected tab.
- storage: session connection/page handoff state.
- nativeMessaging: exchange commands with the separately installed local Argus host.

Data disclosures to review in the dashboard:
Website content, URLs, form input, screenshots, request/response headers and bodies
can be accessed during a connected session. Traffic may include cookies, tokens,
authentication information and personal/financial data depending on open pages.
No Argus developer telemetry endpoint is configured. Data is passed to the local
Agent environment; that Agent or its model provider may process it remotely under
its own configuration and policies. Do not claim that all processing stays local.

Support: https://github.com/WilliamSkyWalker/argus/issues
Privacy source: `distribution/PRIVACY.md` — publish as a publicly accessible HTTPS
page and enter that URL in the dashboard before submission.

Submission checklist (not yet performed):
- Developer account and store-assigned extension ID.
- Public privacy policy URL and matching dashboard data disclosures.
- Real screenshots of the final popup/connected test tabs at store-required sizes.
- Upload the store ZIP, choose Unlisted for beta, request review.
- After approval, test clean installation/update/removal using the store build.
