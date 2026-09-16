# TradingView to MT5 Trade Bridge

**FREE & Open Source** - Automated trading system that detects trades from TradingView Strategy Tester and executes them on MetaTrader 5 (MT5) via **HTTP bridge** or **File I/O** in real-time.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/)
[![MT5](https://img.shields.io/badge/MT5-Compatible-green.svg)](https://www.metatrader5.com/)
[![Status](https://img.shields.io/badge/Status-Active-success.svg)](https://github.com/niiisho/TradingView-MT5-Bridge)

---

## ⚠️ RISK WARNING — READ BEFORE USE

This software executes real trades on your broker account automatically. Automated trading carries significant financial risk. You can lose your entire account balance, including amounts beyond your initial investment.

- The author is NOT responsible for any financial losses, account liquidations, margin calls, or damages of any kind resulting from use of this software.
- This is NOT financial advice and NOT a trading strategy. It is a technical automation tool only.
- Past performance in TradingView's Strategy Tester does NOT guarantee future results on a live account.
- Slippage, broker requotes, network latency, and platform outages can cause execution to differ significantly from what the backtester shows.
- Verify that automated/algorithmic trading is permitted by your broker's Terms of Service before use.
- Always test on a demo account first. Even after demo testing, live results may differ substantially.

By using this software, you accept full and sole responsibility for all trading decisions and their financial consequences.

---

## 🌟 Why This Project is Different

### ✨ **ABSOLUTELY FREE - NO TRADINGVIEW PREMIUM NEEDED!**

**Unlike paid solutions requiring:**
- ❌ TradingView Premium webhooks
- ❌ Cloud VPS ($10-50/month)
- ❌ Complex integrations

**This is:**
- ✅ **100% FREE** - No subscriptions
- ✅ **Multiple Input Methods** - TradingView Chrome Extension OR File I/O
- ✅ **Lightning Fast** - <500ms latency (Chrome) or ~2.75s (File I/O)
- ✅ **Tiny Memory ~45MB** - Minimal resource usage
- ✅ **Production Ready** - Waitress WSGI server
- ✅ **Smart Protection** - Refresh and Rapid Trades Safe
- ✅ **Flexible** - Works with any signal source

---

## 📖 How It Works

### **Option 1: TradingView Chrome Extension (Fast)**

```
TradingView Strategy Tester
         ↓
   Chrome Extension Detects
         ↓ HTTP POST
   localhost:8080 
         ↓ JSON Poll
   MT5 WebRequest EA Executes
         ↓
✅ Trade on Your Broker
```

**Total Latency:** **<500ms** end-to-end!

---

### **Option 2: File I/O Mode (Flexible)**

```
Your Signal Source (Bot/Webhook/Script)
         ↓
   Write JSON to C:\Trades\signals\
         ↓ (checks every 2 seconds)
   Bridge detects file
         ↓ JSON Poll
   MT5 WebRequest EA Executes
         ↓
✅ Trade on Your Broker
```

**Total Latency:** **~2.75 seconds** end-to-end!

**Perfect for:**
- Custom trading bots
- Non-TradingView signal sources
- Webhook integrations
- Testing and automation

👉 **See [FILE_IO_GUIDE.md](FILE_IO_GUIDE.md) for File I/O setup and examples**

---

## Installation & Setup Video
- 3-minute full Installation & Setup Video of Trading-MT5-Bridge to automate Trades with Live Trade Example:
  https://youtu.be/Op9VwIgxM8o

---

## 🎯 Features

- **Dual Input Methods** - TradingView Chrome Extension OR JSON File I/O  
- **Real-time Detection** - Monitors TradingView trades list (Extension) or folder (File I/O)
- **Automatic Signal Extraction** - Extracts BUY/SELL signals with SL/TP/LOT parameters 
- **HTTP Bridge** - localhost:8080 API for all modes
- **Signal Rejection** - Prevents overwrites during processing    
- **Smart Filtering** - Ignores refresh/false signals  
- **Robust Recovery** - Auto-reconnects on tab close  
- **Clean UI** - Real-time logging dashboard  
- **Backward Compatible** - Both HTTP and File modes work simultaneously

---

## 📋 Requirements

- **Windows OS** (MT5)
- **MetaTrader 5**
- **TradingView** Free account (no premium needed!)
- **Python 3.8+** (if running from source)
- **Broker permission** — Confirm your broker's Terms of Service allows automated/algorithmic trading before live use

**For TradingView Chrome Extension mode:**
- Chrome browser

**For File I/O mode:**
- Any process that can write JSON files (Python, C#, Node.js, webhooks, etc.)

---

## 🛠️ Installation

### **Quick Start (Pre-built EXE)**

#### **1. TradingBridge.exe**
1. Run `TradingBridge.exe`
2. ✅ Click "Allow access" when Windows asks
3. ✅ If Blocked: Windows Defender → Allow through firewall

#### **2. Choose Your Mode**

**🔹 Mode A: TradingView Chrome Extension (Recommended for TradingView users)**

1. Chrome → `chrome://extensions/` → **Developer mode**
2. **Load unpacked** → Select `Tradingview_Trade_Detector-Extension` folder
3. ✅ Extension ready!
4. Open TradingView → Strategy Tester → Click **List of Trades** tab
5. Click **View Site Information** → Allow **Local Network Access**

**🔹 Mode B: File I/O (Recommended for custom bots)**

1. Files are automatically written to: `C:\Trades\signals\`
2. No additional setup needed!
3. See [FILE_IO_GUIDE.md](FILE_IO_GUIDE.md) for examples

#### **3. MT5 EA Setup (Same for both modes)**
1. Move `Trading_Bot` Folder to:
```
C:\Users\YourUsername\AppData\Roaming\MetaTrader 5\MQL5\Experts\
```
2. **Add URL:** In MT5 → Tools → Options → Expert Advisors → `http://127.0.0.1:8080`
3. Attach EA to chart → Tick **Allow Algo Trading**
4. Enable **Algo Trading** (Green) → Button on Top Row

---

## 🚀 Usage

### **TradingView Chrome Extension Mode**

1. Start `TradingBridge.exe` - ✅ Shows: localhost:8080 running
2. Load Chrome extension
3. Open TradingView → Strategy Tester → **List of Trades**
4. Open MT5 → **Attach EA** to any chart  
5. ✅ **Automation active!**  
6. ⚠️ **Important:** Keep TradingView tab visible (extension needs it active)

### **File I/O Mode**

1. Start `TradingBridge.exe`
2. Drop JSON files into `C:\Trades\signals\` folder
3. Bridge automatically processes files and sends to MT5
4. Check dashboard logs for execution confirmation

**Example signal file:**
```json
{
  "signal": "BUY SL=50 TP=100 LOT=0.01",
  "timestamp": "2025-09-16T14:30:45Z"
}
```

👉 **Full examples in [FILE_IO_GUIDE.md](FILE_IO_GUIDE.md)**

---

### TradingView Pine Script Setup (Optional)

#### For Variable SL/TP/LOT:

Add this code to your Pine Script strategy to send **dynamic SL/TP/LOT** values:

**Entry name format required:**
```
"BUY SL=<value> TP=<value> LOT=<value>"
"SELL SL=<value> TP=<value> LOT=<value>"
```

**Example implementation:**

```pine
// In your Long entry logic:
if (buyCondition)
    // Calculate your SL/TP/LOT dynamically
    sl_pips = math.round((entry - stop_loss) / pipsize)
    tp_pips = math.round((take_profit - entry) / pipsize)
    lot_size = calculated_lot_size
    
    // Format entry name with SL/TP/LOT
    entry_name = " BUY SL=" + str.tostring(sl_pips) + " TP=" + str.tostring(tp_pips) + " LOT=" + str.tostring(lot_size) + " "
    
    strategy.entry(entry_name, strategy.long, qty=lot_size)
```

Similarly in your Short Entry Logic  
**Values should be in PIPS** (not price levels).


#### Without Variable SL/TP/LOT:

If you don't modify your strategy  
The MT5 EA will use **default values** from inputs.


---

## 📊 Signal Format

```
Full: "BUY SL=50 TP=100 LOT=0.01"
Simple: "BUY" or "SELL"
Fallback: "long"→BUY, "short"→SELL
```

---

## 📁 Structure

```
tradingview-mt5-bridge/
├── TradingBridge.exe                              # HTTP server (pre-built)
├── Trading_Bridge_Source/                         # Python source for the server
│   └── bridge.py                                  # Updated with File I/O support
├── Trading_Bot/                                   # MT5 EA
│   ├── Trading_Bot.mq5
│   └── Trading_Bot.ex5
├── Tradingview_Trade_Detector-Extension/          # Chrome extension (optional)
│   ├── manifest.json
│   ├── content.js
│   ├── logger.html
│   └── logger.js
├── FILE_IO_GUIDE.md                              # Complete File I/O documentation
├── README.md
├── DISCLAIMER.md
└── LICENSE
```

---

## 🔧 Troubleshooting

### **TradingView Chrome Extension Mode**

**Extension not detecting trades:**
- Refresh TradingView page
- Verify "List of Trades" tab is open

**Server errors:**

❌ "Port 8080 already in use"   →  Close other apps using port 8080  
❌ "Server offline"      →   Restart TradingBridge.exe  
❌ "WebRequest error"     →   Add http://127.0.0.1:8080 to MT5 → Tools → Options

**MT5 not trading:**

❌ AutoTrading red? → Click to enable (green)  
❌ Experts tab errors? → Check logs  
❌ Broker restrictions? → Test demo account  

**Rejected/False signals:**

✅ Extension ignores TradingView refresh  
✅ Server rejects pending signals  
✅ If Server **Rejecting Legit Trades** - Try `Clear Old Signal` button    
✅ EA checks existing positions  
✅ No Multiple Trades - Only 1 at a Time Allowed  

### **File I/O Mode**

👉 See [FILE_IO_GUIDE.md - Troubleshooting](FILE_IO_GUIDE.md#troubleshooting) for detailed solutions

**Quick fixes:**
- Verify JSON syntax (use online validator)
- Check `C:\Trades\signals\` folder exists
- Ensure folder has write permissions
- Check TradingBridge GUI logs for errors

---

## ⚙️ Configuration

### **MT5 EA Inputs:**
```
LotSize=0.01
StopLossPoints=50
TakeProfitPoints=100
MagicNumber=12345
ServerURL=http://127.0.0.1:8080/signal
```

### **Bridge Server (bridge.py):**

For File I/O mode, edit `bridge.py` to change folder path:
```python
SIGNALS_FOLDER = r"C:\Trades\signals"  # Change this line
CHECK_INTERVAL = 2  # Check every 2 seconds (change for faster/slower)
```

---

## ❓ FAQ

**Q: Does this work with live trading?**
A: It can connect to a live account technically. However, all financial losses on live accounts are entirely your responsibility. Do not use on a live account until you have tested thoroughly on a demo account first.

**Q: Can I modify SL/TP after trade opens?**
A: Yes, but it's recommended to modify before attaching to chart.

**Q: Multiple strategies same time?**
A: One instance per symbol recommended.

**Q: Can I use both Chrome Extension and File I/O at the same time?**
A: Yes! Both methods can run simultaneously. They share the same signal queue, so use whichever is most convenient.

**Q: How do I switch between modes?**
A: Both modes run automatically. Keep the Chrome extension loaded for Chrome mode, and drop JSON files in the folder for File I/O mode.

**Q: What's the latency difference?**
A: Chrome Extension: <500ms | File I/O: ~2.75 seconds

---

## 🔒 Security

✅ **100% Local** - localhost only   
✅ **No cloud** - No external servers  
✅ **Open Source** - Full transparency  
✅ **No API Keys** - No third-party services  
✅ **Your Credentials** - Stay on your machine only  

---

## ⚠️ Disclaimer and Legal Notice

This software is provided for educational and personal use only. It is a technical bridge tool and does not constitute financial advice, investment advice, or a recommendation to trade any financial instrument.

**Financial Risk:** Automated trading involves substantial risk of financial loss. You may lose more than your initial investment. The author, contributors, and distributors of this software bear no responsibility for any financial consequences resulting from its use.

**No Warranty:** This software is provided "as is" without any warranty of any kind, express or implied. There is no guarantee of accuracy, reliability, fitness for a particular purpose, or uninterrupted operation.

**Not Financial Advice:** Nothing in this software, its documentation, or any associated communications constitutes financial advice. All trading decisions are solely your own responsibility.

**Regulatory Compliance:** It is your responsibility to ensure that your use of automated trading software complies with applicable laws, regulations, and your broker's Terms of Service in your jurisdiction.

**Backtesting vs Live Trading:** Results shown in TradingView's Strategy Tester do not guarantee identical results on a live account. Live markets involve spread, slippage, and execution conditions that differ substantially from backtesting environments.

By downloading, installing, or using this software in any form, you acknowledge that you have read, understood, and agreed to this disclaimer in full.

---

## 📈 Version History

### v2.1.0 (File I/O Release)
✅ Added File I/O mode with JSON signal support  
✅ Implemented folder watcher for automatic signal detection  
✅ Maintained backward compatibility with HTTP POST  
✅ Added FILE_IO_GUIDE.md with comprehensive documentation  
✅ Enhanced GUI to show active mode and watched folder  
✅ Support for hybrid mode (Chrome Extension + File I/O simultaneously)

### v2.0.1
✅ Improved bridge server UI  
✅ Bug fixes and stability improvements  
✅ Python source code now included in repo  
✅ Expanded disclaimer and risk documentation  

---

## 📝 License

**MIT License** - See LICENSE file for details.

**Copyright © 2025 Nishant Prakash Garg. All rights reserved.**


### Using This Project?

✅ **FREE for personal/educational use**   
✅ **Commercial use allowed** (with attribution)   
⚠️ **MUST include** copyright notice and LICENSE file    

---

## 🤝 Contributing

Contributions welcome! Please:

1. **Fork** the repository  
2. **Create** a feature branch (`git checkout -b feature/AmazingFeature`)  
3. **Commit** your changes (`git commit -m 'Add some AmazingFeature'`)  
4. **Push** to the branch (`git push origin feature/AmazingFeature`)  
5. **Open** a Pull Request

---

## 📧 Support & Contact  

**Issues & Questions:**
- 🐛 Open an issue  
- 💬 Check existing issues first 

**Commercial Support:**   
- 🛠️ Installation assistance  
- 📞 Contact: contactme.ngone@gmail.com 

---

## ⭐ Star History  

If this project helped you, please **star the repository!**  

It helps others discover this free alternative to paid services.  

---

**Built with ❤️ by Nishant P.Garg**  

---

### 🚨 Important Note
This is an **independent project** and is **not affiliated** with TradingView, MetaQuotes, or MetaTrader 5.
