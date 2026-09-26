# Browser backend tests

```bash
python3 -m unittest discover -s tests/browser_demo -v
```

Offline tests cover backend compatibility, page identity and disconnect semantics.
To include the real Chromium integration test, install Playwright and set the Chrome binary:

```bash
ARGUS_TEST_CHROME=/path/to/chrome python3 -m unittest discover -s tests/browser_demo -v
```

The integration test uses a temporary browser profile and a localhost HTTP fixture.
It checks real input, popup discovery, reconnection across processes, persisted page selection,
human handoff/resume and browser survival after controller shutdown. It does not access a real
login provider or payment service. See [browser guide](../../docs/browser.md).
