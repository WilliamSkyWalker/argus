# Mobile lifecycle tests

Run `python3 -m unittest discover -s tests/mobile_demo -v`.
Tests mock platform commands, use temporary directories and perform no downloads
or physical-device operations. See `docs/mobile.md` for deployment requirements
and the distinction between tested command logic and hardware validation.
