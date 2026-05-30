# FUSE Energy — Home Assistant Custom Integration

Home Assistant custom integration for [FUSE Energy](https://www.fuseenergy.com) energy and gas reporting.

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS:
   `https://github.com/andrejkurlovic/fuse_energy`
2. Install the "FUSE Energy" integration
3. Restart Home Assistant

### Manual

Copy the `custom_components/fuse_energy/` directory into your Home Assistant `custom_components/` directory.

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for "FUSE Energy"
3. Enter your FUSE Energy account email
4. Enter the OTP verification code sent to your phone/email
5. The integration will create sensors for balance, consumption, tariff, bill, and more

## Sensors

| Sensor | Description |
|---|---|
| Balance | Current account balance |
| Energy consumption | Electricity consumption (kWh) |
| Gas consumption | Gas consumption (kWh) |
| Current tariff | Tariff name |
| Standing charge | Daily standing charge |
| Unit rate | Electricity unit rate |
| Direct debit status | Direct debit status |
| Bill amount | Current bill amount |

## Development

```bash
# Lint
ruff check custom_components/

# Type check
pyright custom_components/
```

## API Source

Extracted from FUSE Energy APK v2.0.65 via JADX decompilation. All endpoints documented in `ai-working/FUSE_API_MAP.md`.
