# gcode
This repo is for an application that creates G-code from input name and number in Persian font and sends it to a laser engraving machine.

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
- The generated G-code uses `G0` for rapid moves, `G1` for engraving moves, and `M03`/`M05` to toggle laser power.
- Use a Persian-capable font installed on Windows for best results.
