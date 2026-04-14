# Setup A: Current PC (local test)

```bat
cd /d "C:\Users\hlu\Desktop\Kitchen printer\KitchenPrint-Pro"
python -m pip install Flask pywin32
python app.py
```

- PC: `http://127.0.0.1:5000/`
- Phone/tablet (same Wi‑Fi): `http://<PC IPv4>:5000/`

Find IPv4:

```bat
ipconfig
```

Allow LAN access (Windows Firewall):

```bat
netsh advfirewall firewall add rule name="KitchenPrint 5000" dir=in action=allow protocol=TCP localport=5000
```


# Setup B: Restaurant PC (fresh install)

## 1) Install Git + Python

```bat
winget install -e --id Git.Git
winget install -e --id Python.Python.3.12
```

## 2) Download app

```bat
cd /d "%USERPROFILE%\Desktop"
mkdir "Kitchen printer"
cd "Kitchen printer"
git clone https://github.com/Radot1/KitchenPrint-Pro.git
cd KitchenPrint-Pro
```

## 3) Install packages

```bat
python -m pip install Flask pywin32
```

## 4) Configure printers

- Open the app: `http://127.0.0.1:5000/`
- Click ⚙️
- Select default printer
- Set **Category → Printer** mapping

## 5) Allow phones/tablets

```bat
netsh advfirewall firewall add rule name="KitchenPrint 5000" dir=in action=allow protocol=TCP localport=5000
```

## 6) Run

```bat
cd /d "%USERPROFILE%\Desktop\Kitchen printer\KitchenPrint-Pro"
python app.py
```

Open from other devices:

```bat
ipconfig
```

Use:

`http://<PC IPv4>:5000/`

