"""Used by build.bat: reads the app version from ../version.py, writes the
Windows version resource for the .exe (build/version_info.txt) and prints the
version so build.bat can hand it to the installer. One source of truth."""
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(HERE, "..", "version.py"), encoding="utf-8").read()
version = re.search(r'__version__\s*=\s*"([^"]+)"', src).group(1)
nums = (tuple(int(x) for x in version.split(".")) + (0, 0, 0, 0))[:4]

os.makedirs(os.path.join(HERE, "build"), exist_ok=True)
with open(os.path.join(HERE, "build", "version_info.txt"), "w", encoding="utf-8") as f:
    f.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={nums}, prodvers={nums}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041F04B0', [
      StringStruct('CompanyName', 'YUZDOKSANBES'),
      StringStruct('FileDescription', 'YUZDOKSANBES POS - kafe siparis sistemi'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('InternalName', 'YUZDOKSANBES_POS'),
      StringStruct('OriginalFilename', 'YUZDOKSANBES_POS.exe'),
      StringStruct('ProductName', 'YUZDOKSANBES POS'),
      StringStruct('ProductVersion', '{version}')])]),
    VarFileInfo([VarStruct('Translation', [0x041F, 1200])])
  ]
)
""")
print(version)
