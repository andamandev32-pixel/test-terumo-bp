@echo off
cd "C:\Program Files (x86)\com0com"
setupc.exe change CNCA0 PortName=COM8
setupc.exe change CNCB0 PortName=COM9
setupc.exe change COM# PortName=COM8
setupc.exe change COM# PortName=COM9
exit
