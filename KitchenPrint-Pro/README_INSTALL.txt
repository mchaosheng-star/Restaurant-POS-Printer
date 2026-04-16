KitchenPrint-Pro Restaurant Install Guide

This file explains how to install and run KitchenPrint-Pro on a restaurant Windows PC.

What this program does

1. Runs a local web app on the restaurant PC.
2. Lets staff use the webpage for manual orders.
3. Creates a virtual AirPrint printer so iPhone/iPad can print order slips into the program.
4. Reads the printed order, extracts food items and customer note, and sends items to the correct Kitchen or Sushi printer.


Requirements

1. A Windows PC that stays on during service.
2. Python 3 installed.
3. The PC and iPhone/iPad must be on the same network.
4. Real kitchen printers already installed in Windows.
5. Internet is recommended for first-time Python package install.


Part 1. Install Python

1. Open PowerShell.
2. Check Python:

   python --version

3. If Python is not installed:
   - Download Python from https://www.python.org/downloads/windows/
   - During install, check "Add Python to PATH"
   - Finish install
4. Open a new PowerShell window.
5. Check again:

   python --version


Part 2. Put the project on the PC

1. Copy the project folder to the PC.
2. Open PowerShell.
3. Go into the project folder:

   cd D:\Documents\GitHub\Restaurant-POS-Printer\KitchenPrint-Pro

4. If your path is different, use your real path instead.


Part 3. Install program dependencies

Run:

python -m pip install -r requirements.txt

This installs:
- flask
- pywin32
- ippserver
- zeroconf
- pypdf
- pymupdf
- pillow
- rapidocr-onnxruntime
- pytesseract
- psutil

Note:
- `rapidocr-onnxruntime` is already used for OCR.
- `pytesseract` is installed too, but Windows Tesseract program is optional.


Part 4. Find the real printer names in Windows

Run:

python -c "import win32print; print('\n'.join(str(p[2]) for p in win32print.EnumPrinters(2)))"

Write down the exact printer names for:
- Kitchen printer
- Sushi printer


Part 5. Find the PC local IP and network interface index

1. Find the PC IPv4 address:

   ipconfig

2. Use the IPv4 on the restaurant LAN.
   Example:
   192.168.0.02

3. Find the interface index for that IP:

   Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceIndex,IPAddress,InterfaceAlias | Format-Table -AutoSize

4. Look for the row matching your LAN IP.
   Example:
   InterfaceIndex 20   IPAddress 192.168.0.35

5. Write down:
   - LAN IP
   - InterfaceIndex


Part 6. Open Windows Firewall ports

Run these commands in PowerShell:

netsh advfirewall firewall add rule name="KitchenPrint AirPrint mDNS" dir=in action=allow protocol=UDP localport=5353
netsh advfirewall firewall add rule name="KitchenPrint IPP" dir=in action=allow protocol=TCP localport=8631
netsh advfirewall firewall add rule name="KitchenPrint Web UI" dir=in action=allow protocol=TCP localport=5000


Part 7. Start the program

In PowerShell, run:

cd D:\Documents\GitHub\Restaurant-POS-Printer\KitchenPrint-Pro
$env:PRINT_CAPTURE_MDNS_HOST="192.168.0.35"
$env:PRINT_CAPTURE_INTERFACE_INDEX="20"
$env:PRINT_CAPTURE_AIRPRINT_NAME="KitchenPrintPro"
python app.py

Replace:
- 192.168.0.35 with your real LAN IP
- 20 with your real InterfaceIndex

Keep this PowerShell window open while the restaurant is using the system.


Part 8. Open the web app

On the PC browser open:

http://127.0.0.1:5000/

If you want to open it from another device on the same network:

http://YOUR_PC_IP:5000/

Example:

http://192.168.0.35:5000/


Part 9. Configure printer routing in Settings

1. Open the webpage.
2. Click the gear icon.
3. In Printer Assignment:
   - Set Kitchen Printer = the real kitchen printer
   - Set Sushi Printer = the real sushi printer
4. In Category To Printer:
   - Set each category to either Kitchen or Sushi
5. In Sushi Add-on Route:
   - Choose Kitchen or Sushi
6. Close settings.

Important:
- The top settings choose the actual device names.
- The category rows only choose Kitchen or Sushi.


Part 10. Test manual order routing from the webpage

1. In the webpage, add a few test items from different categories.
2. Click Send.
3. Check:
   - Kitchen items go to the kitchen printer
   - Sushi items go to the sushi printer


Part 11. Test iPhone/iPad AirPrint import

1. Make sure iPhone/iPad is on the same network as the PC.
2. On iPhone/iPad, open any printable page.
3. Tap Share -> Print.
4. Select printer:
   KitchenPrintPro
5. Print.

The program should receive the print job and turn it into an incoming order.


Part 12. How the restaurant should use it

Normal flow:

1. Staff accepts the order in Uber / DoorDash / Grubhub app.
2. Staff taps Print in the official app.
3. Print goes to KitchenPrintPro AirPrint printer.
4. KitchenPrint-Pro extracts:
   - food items
   - quantities
   - customer note
5. Order appears in Incoming Orders on the webpage.
6. Staff taps Accept.
7. Items are matched to your menu.
8. Each item is sent to the assigned Kitchen or Sushi printer.


Part 13. Virtual test mode without real printers

If you want to test routing without printing to real devices:

cd D:\Documents\GitHub\Restaurant-POS-Printer\KitchenPrint-Pro
$env:VIRTUAL_PRINT="1"
$env:PRINT_CAPTURE_MDNS_HOST="192.168.0.35"
$env:PRINT_CAPTURE_INTERFACE_INDEX="20"
$env:PRINT_CAPTURE_AIRPRINT_NAME="KitchenPrintPro"
python app.py

Then after you accept an order, preview files are written to:

data\virtual_prints

Each preview file shows which printer the ticket would go to.


Part 14. If AirPrint printer does not appear on iPhone/iPad

Check these:

1. PC and iPhone/iPad are on the same network.
2. Windows firewall rules were added.
3. The program is currently running.
4. LAN IP and InterfaceIndex are correct.
5. Close the iPhone print picker and reopen it.
6. Wait 30-60 seconds for cache refresh.

If old duplicate printer names are stuck on the phone, use a new AirPrint name:

$env:PRINT_CAPTURE_AIRPRINT_NAME="KitchenPrintPro2"
python app.py


Part 15. If items are wrong or too much text is captured

1. Print a real receipt from the official delivery app.
2. Check the Incoming order on the webpage.
3. The program is designed to keep:
   - item names
   - quantities
   - customer note
4. It tries to ignore:
   - phone number
   - address
   - fees
   - tax
   - tip
   - delivery instructions not part of the food order

If a marketplace changes its print format, parser rules may need updating.


Part 16. Daily startup

Every day:

1. Turn on the restaurant PC.
2. Open PowerShell.
3. Run:

   cd D:\Documents\GitHub\Restaurant-POS-Printer\KitchenPrint-Pro
   $env:PRINT_CAPTURE_MDNS_HOST="192.168.0.35"
   $env:PRINT_CAPTURE_INTERFACE_INDEX="20"
   $env:PRINT_CAPTURE_AIRPRINT_NAME="KitchenPrintPro"
   python app.py

4. Leave the window open.
5. Open the webpage at:

   http://127.0.0.1:5000/


Part 17. Optional: create a startup batch file

Create a file named `Start-KitchenPrint.bat` with this content:

cd /d D:\Documents\GitHub\Restaurant-POS-Printer\KitchenPrint-Pro
set PRINT_CAPTURE_MDNS_HOST=192.168.0.35
set PRINT_CAPTURE_INTERFACE_INDEX=20
set PRINT_CAPTURE_AIRPRINT_NAME=KitchenPrintPro
python app.py
pause

Then double-click that file each day.


Part 18. Optional: install Tesseract for extra OCR fallback

This is optional.
The program already uses RapidOCR.

If you want Tesseract too:

1. Download Tesseract for Windows
2. Install it
3. Make sure `tesseract.exe` is in PATH
4. Restart PowerShell


Part 19. Important folders

Main web app:
- Sakura.html

Menu:
- data\menu.json

Order history:
- data\orders_YYYY-MM-DD.csv

Captured AirPrint jobs:
- data\print_jobs

Virtual test output:
- data\virtual_prints


Part 20. Quick troubleshooting

Problem: webpage opens but no printer on iPhone
- Check firewall
- Check IP and interface index
- Check app is running

Problem: order appears but wrong items
- Test with a real official app print
- OCR/parser may need adjustment for that marketplace format

Problem: accepted order does not split to Kitchen / Sushi
- Check category routing in Settings
- Check top Kitchen Printer and Sushi Printer are assigned
- Check item names exist in your menu

Problem: nothing prints
- Verify Windows printer names
- Try virtual mode first


End of guide
