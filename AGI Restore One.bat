@echo off
chcp 65001 >nul 2>&1
echo.
echo  +----------------------------------------------------------+
echo  ^|      Antigravity - Restore Satu Conversation            ^|
echo  ^|      [Mode Khusus: Antigravity IDE]                     ^|
echo  +----------------------------------------------------------+
echo.
echo  PENTING: Tutup Antigravity sepenuhnya sebelum melanjutkan!
echo.
pause
echo.
python "%~dp0restore_one.py"
echo.
pause

