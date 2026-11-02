# Sources and attribution

KB22 Remote is an independent implementation for operating a TOPPING KB22 over its USB HID interface. It does not require or include a vendor application, driver installer, firmware image, disassembly, or vendor source code.

## Interface resources

The public edition uses operating-system fonts and SVG icons drawn for this project, covered by the project MIT license. An earlier local prototype used resources extracted from Focusrite Control 2; those files and screenshots have been excluded from this repository. The dark mixer layout was informed by that control surface and by KB Control Center workflows. No vendor logo is included.

## Protocol fixtures

- `tests/fixtures/native-wire.json` contains parameter inputs and corresponding wire-frame outputs observed from the KB Control Center serializer. Its native binary SHA-256 identifies the program version used to obtain the observations; the binary itself is not distributed.
- `native-reset.json` contains three observed reset-command frames. Automated tests compare bytes in memory and never send reset commands to hardware.
- `pro-upload.json` and `hifi-upload.json` contain recorded configuration report frames for parser regression tests. They contain numerical device settings, not audio recordings, credentials, or USB serial numbers.
- `playback-live.json` contains numerical meter reports from a controlled low-level test signal; it contains no PCM audio.
- `validation/` contains summarized historical measurements, not live connection settings or diagnostic logs.

These fixtures document interoperability behavior. The project license does not grant rights to any third-party trademarks or proprietary application resources. TOPPING, KB22, Focusrite and other referenced product names belong to their respective owners. This project is not affiliated with, sponsored by, or endorsed by those companies.
