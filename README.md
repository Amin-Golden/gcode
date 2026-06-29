# gcode
This repo is for an application that creates single-line laser G-code from Persian name fields and bank-card fields, then saves or sends it to a Marlin 2 laser engraving machine.

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
- Card number, expiry date, and CVV2 are normalized to English digits in the generated output.
- The generated G-code targets Marlin 2 laser mode and uses `G0`, `G1`, `M3 S...`, and `M5`.
