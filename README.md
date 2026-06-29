# gcode
This repo is for an application that creates laser G-code from Persian name, IBAN, and bank-card fields, then saves or sends it to a GRBL-M3 laser engraving machine.

## Requirements
- Python 3.10+
- PyQt6
- pyserial

## Install
```powershell
py -3 -m pip install -r requirements.txt
```

## Run
```powershell
py -3 laser_text_gcode.py
```

## Notes
- The GUI shows the engraving output before generating or sending G-code.
- You can set the position of each text item with X/Y fields or by dragging it in the preview.
- Persian fields use B Nazanin and numeric fields use Calibri.
- G-code uses vector outlines extracted from the same displayed fonts, so the engraved shape matches the UI preview much more closely.
- Standard fonts such as B Nazanin and Calibri are outline fonts; exact visual matching requires tracing their outlines or using scan/fill mode.
- IBAN, card number, expiry date, and CVV2 are normalized to English digits/letters in the generated output.
- The generated G-code targets GRBL-M3 with `transferMode: buffered`, `S-value max: 255`, homes with `$H`, starts from the configured card origin, and uses `G0`, `G1`, `M3`, `S...`, and `M5`.
- Output toolpaths are rotated 180 degrees around the configured work area before being written to G-code.
- The card origin is fixed at X0.0 Y30.0 on a 105 x 90 mm laser bed, and the card outline test generates a rectangle around the configured 86 x 54 mm card area.
- The app estimates engraving time from generated G-code movement lengths and feed rates.
- During serial sending, the UI shows command-send progress as a percentage.
