KitchenPrint-Pro Restaurant Install (Windows)

Goal
- One PC receives delivery-app prints (AirPrint) and prints 3 tickets per order:
  1) Original -> Packer Printer
  2) Kitchen (+ sushi add-ons) -> Kitchen Printer
  3) Sushi -> Sushi Printer

Restaurant LAN IPv4 (this PC)
- 192.168.15.135


1) Install Python 3
1. Download Python for Windows.
2. Install and check:
   - MUST enable: "Add Python to PATH"
3. Open PowerShell and verify:

python --version


2) Get the project onto the PC
1. Copy this folder to the PC:
   Restaurant-POS-Printer\KitchenPrint-Pro
2. Open PowerShell and go to the folder, example:

cd "C:\Users\hlu\Documents\GitHub\Restaurant-POS-Printer\KitchenPrint-Pro"


3) Install dependencies
In PowerShell (inside KitchenPrint-Pro):

python -m pip install -r requirements.txt

py -m pip install ippserver
setx PRINT_CAPTURE_MDNS_HOST "192.168.1.50"

`````````````````````````````
winget search airprint
winget search bonjour

winget install --id Apple.Bonjour -e
# 把 <...> 换成真实 id（不要带尖括号），例如：
# winget install --id Some.Publisher.AirPrintInstaller -e



winget install --id Apple.BonjourPrintServices -e
# 你现在只有虚拟打印机；把 KitchenPrint-Pro 要用的厨房打印机装到 Windows（厂商驱动/网络打印机）
# 装好后确认它出现在列表里
Get-Printer | ft Name,DriverName,PortName

``````````````````````````````````````````````````


4) Install your real printers in Windows
1. Add/install the real printers (Kitchen / Sushi / Packer) in Windows first.
2. List printer names (optional, for exact spelling):

python -c "import win32print; print('\n'.join(str(p[2]) for p in win32print.EnumPrinters(2)))"


5) Open Windows Firewall ports (run as Admin PowerShell)

netsh advfirewall firewall add rule name="KitchenPrint AirPrint mDNS" dir=in action=allow protocol=UDP localport=5353
netsh advfirewall firewall add rule name="KitchenPrint IPP" dir=in action=allow protocol=TCP localport=8631
netsh advfirewall firewall add rule name="KitchenPrint Web UI" dir=in action=allow protocol=TCP localport=5000


6) Start the program (PowerShell)
Open PowerShell and run:

cd "C:\Users\hlu\Documents\GitHub\Restaurant-POS-Printer\KitchenPrint-Pro"
$env:PRINT_CAPTURE_MDNS_HOST="192.168.15.135"
$env:PRINT_CAPTURE_AIRPRINT_NAME="KitchenPrintPro"
python .\app.py

Keep this window open during service.

If AirPrint doesn’t show up on iPhone/iPad, also set InterfaceIndex:
1) Find InterfaceIndex for 192.168.15.135:

Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceIndex,IPAddress,InterfaceAlias | Format-Table -AutoSize

2) Start with InterfaceIndex (example 20):

$env:PRINT_CAPTURE_MDNS_HOST="192.168.15.135"
$env:PRINT_CAPTURE_INTERFACE_INDEX="20"
$env:PRINT_CAPTURE_AIRPRINT_NAME="KitchenPrintPro"
python .\app.py


7) Open the web app
On the PC:

http://127.0.0.1:5000/

From another device on the same LAN:

http://192.168.15.135:5000/


8) Configure printers (required)
1. Open the webpage.
2. Click the gear icon.
3. Set:
   - Kitchen Printer = your real kitchen printer
   - Sushi Printer = your real sushi printer
   - Packer Printer = your original/full-order printer
4. Close settings.


9) Test (no delivery apps)
Enable virtual print preview:

cd "C:\Users\hlu\Documents\GitHub\Restaurant-POS-Printer\KitchenPrint-Pro"
$env:VIRTUAL_PRINT="1"
$env:PRINT_CAPTURE_MDNS_HOST="192.168.15.135"
$env:PRINT_CAPTURE_AIRPRINT_NAME="KitchenPrintPro"
python .\app.py

After accepting an order, previews are saved to:
data\virtual_prints


10) Test (delivery app AirPrint)
1. iPhone/iPad must be on the same Wi‑Fi/LAN as the PC.
2. In Uber Eats / DoorDash / Grubhub app, open an order and tap Print.
3. Select printer:
   KitchenPrintPro
4. The order appears in Incoming Orders on the webpage.
5. Tap Accept.
6. It should print 3 papers:
   - Original -> Packer Printer
   - Kitchen (+ sushi add-ons) -> Kitchen Printer
   - Sushi -> Sushi Printer


Important folders
- Menu: data\menu.json
- Captured print jobs: data\print_jobs
- Virtual print previews: data\virtual_prints
- Order history CSV: data\orders_YYYY-MM-DD.csv
