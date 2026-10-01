"""Isolated opt-in GUI fixture; writes only to the supplied test directory."""
import json
from pathlib import Path
import sys

import AppKit as A
from Foundation import NSObject, NSProcessInfo, NSTimer

root = Path(sys.argv[1])
state = {"clicks": 0, "text": "", "scrolls": 0}


def save():
    temporary = root / "fixture-state.tmp"
    temporary.write_text(json.dumps(state, ensure_ascii=False))
    temporary.replace(root / "fixture-state.json")


class Handler(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        window.orderFrontRegardless()
        window.makeFirstResponder_(field)
        self.timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            .05, self, "sampleScroll:", None, True)
        save()
        (root / "ready.json").write_text(json.dumps({
            "app": NSProcessInfo.processInfo().processName(),
            "pid": NSProcessInfo.processInfo().processIdentifier(),
            "window_id": window.windowNumber(),
        }))

    def clicked_(self, sender):
        state["clicks"] += 1
        sender.setTitle_(f'Clicks: {state["clicks"]}')
        save()

    def controlTextDidChange_(self, notification):
        state["text"] = str(field.stringValue())
        save()

    def sampleScroll_(self, timer):
        state["scrolls"] = float(scroll.contentView().bounds().origin.y)
        save()


app = A.NSApplication.sharedApplication()
app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)
window = A.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
    ((700, 300), (400, 300)),
    A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable | A.NSWindowStyleMaskNonactivatingPanel,
    A.NSBackingStoreBuffered, False)
window.setReleasedWhenClosed_(False)
window.setTitle_("Saygo Background Test")
window.setLevel_(A.NSFloatingWindowLevel)
window.setCollectionBehavior_(A.NSWindowCollectionBehaviorCanJoinAllSpaces |
                             A.NSWindowCollectionBehaviorFullScreenAuxiliary)
handler = Handler.alloc().init()
button = A.NSButton.alloc().initWithFrame_(((40, 200), (150, 45)))
button.setTitle_("Clicks: 0")
button.setTarget_(handler)
button.setAction_("clicked:")
window.contentView().addSubview_(button)
field = A.NSTextField.alloc().initWithFrame_(((40, 120), (300, 40)))
field.setDelegate_(handler)
window.contentView().addSubview_(field)
scroll = A.NSScrollView.alloc().initWithFrame_(((40, 10), (300, 80)))
scroll.setHasVerticalScroller_(True)
scroll.setScrollerStyle_(A.NSScrollerStyleLegacy)
document = A.NSTextView.alloc().initWithFrame_(((0, 0), (270, 1500)))
document.setString_("\n".join(f"Test row {i:03d}" for i in range(100)))
document.setEditable_(False)
scroll.setDocumentView_(document)
window.contentView().addSubview_(scroll)
app.setDelegate_(handler)
app.run()
