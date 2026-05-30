# FUSE Energy API Map
Extracted from APK v2.0.65 (com.fuseenergy)

## Base URLs
| Environment | API Base URL | Website |
|---|---|---|
| **Production** | `https://api.fuseenergy.com` | `https://www.fuseenergy.com` |
| Staging | `https://api.fuseenergy.dev` | `https://www.fuseenergy.dev` |

Source: `ml/d.java` — stored in SharedPreferences under key `BASE_URI`, defaults to production.

## Authentication

### Auth Provider: FUSE custom (NOT OAuth2/Privy)
- Privy (`auth.privy.io`) is used for **embedded wallet/crypto** features only (privy-app-id: `cmdee53eu001njp0n1clt5rzs`)
- Main auth is FUSE's own **v3 challenge-based flow** (email → OTP or magic link)

### Auth Flow (v3 API)
Challenge-response flow via `api/v3/auth/*` (paths from smali `d3/y9.smali`):

1. **Initial Challenge** → send email → get `auth_flow_token`
   - Request: `AuthRequest.InitialChallenge` (data: email)
   - Response: `AuthResponse.OtpChallenge` or `AuthResponse.MagicLinkCheckChallenge`

2. **OTP Check** → send OTP code + `auth_flow_token`
   - Request: `AuthRequest.OtpChallenge` (data: phone_number + otp code)
   - Response: `AuthResponse.AuthorizedChallenge` or next challenge

3. **Magic Link Check** → verify magic link + `auth_flow_token`
   - Request: `AuthRequest.MagicLinkCheckChallenge` (data: email)
   - Response: `AuthResponse.AuthorizedChallenge` or next challenge

4. **Additional Info** → provide missing profile data
   - Request: `AuthRequest.AdditionalInfoChallenge` (data: answers to questions)
   - Response: `AuthResponse.CreateAccountChallenge` or `AuthorizedChallenge`

5. **Create Account** → accept T&Cs
   - Request: `AuthRequest.CreateAccountChallenge` (data: policy_versions acceptance)
   - Response: `AuthResponse.AuthorizedChallenge`

### Token Pair (successful auth response)
```json
{
  "access_token": "string",
  "refresh_token": "string",
  "new_user": true/false
}
```
JSON fields: `access_token`, `refresh_token`, `new_user`

### Token Refresh
Endpoint: `POST api/v1/auth/refresh` (from Retrofit path in smali)
Request body:
```json
{
  "refresh_token": "string",
  "original_request_path": "string (optional)"
}
```
Response: `AuthResponseTokenPair` (access_token, refresh_token, new_user)

### Auth Request/Response Model (v3)
**AuthRequest**: sealed class with `challenge_type` field + `data` object + `auth_token_ttl`
**AuthResponse**: sealed class with `auth_flow_token` field + `data` object

Challenge types: `INITIAL`, `PHONE_OTP`, `MAGIC_LINK_CHECK`, `ADDITIONAL_INFO`, `CREATE_ACCOUNT`

### HTTP Client Setup
- **Retrofit** with OkHttp engine (NOT Ktor for the main API — Ktor is used elsewhere)
- `HttpClientModule` provides:
  - OkHttpClient with headers interceptor + AppCheck interceptor (Firebase App Check)
  - Base URL from SharedPreferences (`BASE_URI`)
- **Bearer token** auth via headers interceptor (adds Authorization header)

## API Endpoints (from FuseApiService.java)

### Core Energy/Gas Data (PRIMARY for HA integration)
| Method | Path | Description | Key Types |
|---|---|---|---|
| `GET` | `api/v2/customer/premises` | Get all premises with supplies | `List<PremisesWithSuppliesNetwork>` |
| `GET` | `api/v1/premises/{premises_fid}/chart` | Consumption chart data (year/month/day) | `ChartResponse` |
| `GET` | `api/v1/premises/{premises_fid}/your-bill` | Bill details per supply | `BillWithCorrectionDetails` |
| `GET` | `api/v1/premises/{premises_fid}/bill-update-info` | Bill update info | `BillUpdateInfo` |
| `GET` | `api/v5/contracts-current` | Current energy/gas contracts | `CurrentContractsResponse` |
| `GET` | `api/v5/contracts-historical` | Historical contracts | `HistoricalContractsResponse` |
| `GET` | `api/v1/tariff/details` | Tariff details per supply | `TariffDetailsResponse` |
| `GET` | `api/v1/tariff/features` | Tariff feature flags | `TariffFeaturesResponse` |
| `POST` | `api/v7/tariffs/available-and-current` | Available & current tariffs | `TariffsAvailableNetwork` |
| `GET` | `api/v1/balance` | Wallet/balance | `Money` |
| `GET` | `api/v1/direct-debit-status` | Direct debit status | `DirectDebitStatusDetails` |

### Billing/Payments
| Method | Path | Description | Key Types |
|---|---|---|---|
| `GET` | `api/v3/payments` | List payments | `PaymentsResponse` |
| `GET` | `api/v3/payment/{payment_id}` | Get single payment | `Payment` |
| `GET` | `api/v1/payment-pill` | Payment summary pill | `PaymentPill` |
| `POST` | `api/v2/payment/topup` | Top up balance | `TopUpDetails` |
| `GET` | `api/v3/statement/whats-included` | Statement breakdown | `StatementWhatsIncludedResponse` |
| `GET` | `api/v1/statement/available-range` | Statement date range | `StatementRange` |
| `GET` | `api/v1/premises/{premises_fid}/occupants` | Get premises occupants | `List<Occupant>` |

### User/Account
| Method | Path | Description | Key Types |
|---|---|---|---|
| `GET` | `api/v1/individual` | Get user profile | `IndividualNetwork` |
| `PATCH` | `api/v1/individual/partial-update` | Update profile | `IndividualNetwork` |
| `GET` | `api/v1/chat/token` | Get Intercom chat token | `ChatToken` |
| `GET` | `api/v1/preferences/alerts` | Get alert preferences | `PreferencesAlertsResponse` |

### Properties/Addresses
| Method | Path | Description | Key Types |
|---|---|---|---|
| `GET` | `api/v4/addresses` | Search addresses | `AddressesResponse` |
| `GET` | `api/v1/properties/{address_id}` | Get property details | `MapFeatureCollection` |

### Notifications
| Method | Path | Description | Key Types |
|---|---|---|---|
| `GET` | `api/v2/notifications/activity-centre` | Activity centre | `AppNotificationsResponse` |
| `GET` | `api/v1/notifications/activity-centre/unread-count` | Unread count | `AppNotificationsCountersResponse` |
| `GET` | `api/v2/notifications/banner` | Banner notifications | `AppNotificationsResponse` |

## Key Data Models

### PremisesWithSuppliesNetwork
- Contains list of premises, each with supplies (electricity import, electricity export, gas)
- Supply has: `SupplyType` (ELECTRICITY_IMPORT, ELECTRICITY_EXPORT, GAS), meter info, switching status

### ChartResponse
- Consumption data for a premises by date range (year required, month/day optional)
- Used for energy usage charts

### CurrentContractsResponse
- Current active energy/gas contracts per supply

### TokenPair
```
access_token: String (@SerializedName("access_token"))
refresh_token: String? (@SerializedName("refresh_token"))
```

### AuthServerData.Authorized (successful auth)
```
access_token: String
refresh_token: String
is_new_user_created: boolean
```

## Authentication Flow for HA Integration
Since FUSE uses a custom challenge-based auth (not standard OAuth2), the HA integration will need:

1. **Config Flow**: User enters email → trigger initial challenge
2. **OTP Step**: User receives OTP (email/SMS) → enter in HA config flow
3. **Token Storage**: Store `access_token` + `refresh_token`
4. **Token Refresh**: Use `POST api/v1/auth/refresh` with `refresh_token` when expired
5. **API Calls**: All API calls use `Authorization: Bearer {access_token}` header

## Sensor Map (HA Integration)
Based on the API endpoints, these sensors can be created:

| Sensor | API Endpoint | Update Interval |
|---|---|---|
| Current Balance | `GET api/v1/balance` | 30min |
| Energy Consumption (daily/monthly) | `GET api/v1/premises/{fid}/chart` | 1hr |
| Current Tariff | `GET api/v1/tariff/details` | 1hr |
| Direct Debit Status | `GET api/v1/direct-debit-status` | 1hr |
| Bill Amount | `GET api/v1/premises/{fid}/your-bill` | 1hr |
| Contract Status | `GET api/v5/contracts-current` | 1hr |

## SSE/Realtime
- Ably SDK used for real-time SSE streaming (`SseStreamClient`)
- Likely for live consumption data and notifications
- Not critical for initial HA integration — polling is sufficient

## GraphQL
- `d3/y9.smali` references `api.fuseenergy.dev` with GraphQL setup
- GraphQL may be used for some features but REST API is the primary interface
- All endpoints in `FuseApiService` are REST (Retrofit annotations)
