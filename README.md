# Scam Call Monitor

A Flask application for automated outbound calling with live audio monitoring, transcription, and admin controls.

## Features

### Core Functionality
- Outbound call attempts with configurable intervals and rate limiting
- Live audio streaming from calls via Twilio Media Streams
- Real-time transcription and conversation monitoring
- Call history with CSV/JSON export
- Rotating opening prompts

### New Features (Latest Update)

#### 1. Admin Login and Settings
- **Admin Access**: Click the "Admin" button in the header
- **Credentials** (development): 
  - Username: `bootycall`
  - Password: `scammers`
- **Session Management**: Secure cookie-based authentication with CSRF protection
- **Environment Variable Editor**: 
  - Modify non-secret configuration at runtime
  - Changes are persisted to `.env` file and applied immediately
  - **Safe Keys Only**: System automatically prevents editing of sensitive keys

**Protected/Secret Keys** (automatically filtered out):
- Keys containing: `TOKEN`, `SECRET`, `KEY`, `PASS`, `PASSWORD`, `PRIVATE`, `SID`
- Specific keys: `TWILIO_AUTH_TOKEN`, `TWILIO_ACCOUNT_SID`, `SECRET_KEY`, `NGROK_AUTHTOKEN`

**Editable Keys** (examples):
- `MAX_CALLS_PER_HOUR`, `CALL_WINDOW_MINUTES`
- `CALLEE_NUMBER`, `CALLER_ID` 
- Feature toggles and UI settings
- `ACTIVE_HOURS_LOCAL`, `ACTIVE_DAYS`

#### 2. Custom Greeting Phrases
- **Add Greeting Phrase**: Button next to "Call now" opens a modal
- **Word Limit**: 5-15 words (validated client-side and server-side)
- **One-Time Use**: Phrase is used for the very next call only, then cleared
- **Integration**: Replaces the default greeting ("Hello. This is an automated assistant from...") 

#### 3. Rate Limiting Notifications
- **Cap Reached**: When hitting max calls per hour/day limits
- **HTTP 429 Response**: Server returns structured error response
- **Toast Notification**: Displays exact message: "Max calls reached in alloted time. Dont over scam the scammer!"
- **No Navigation**: User stays on current page with brief popup notification

#### 4. Matrix Background Animation
- **Visual Effect**: Cascading 0s and 1s animation behind app content
- **Accessibility**: Respects `prefers-reduced-motion: reduce` setting
- **Performance**: Uses `requestAnimationFrame` for smooth animation
- **Non-Intrusive**: Proper z-indexing ensures no interference with UI interaction

## API Endpoints

### Admin Endpoints
```bash
# Login
POST /api/admin/login
Content-Type: application/json
{
  "username": "bootycall",
  "password": "scammers"
}

# Logout  
POST /api/admin/logout

# Get editable configuration
GET /api/admin/config
# Returns: {"config": {"KEY": "value", ...}}

# Update configuration
PUT /api/admin/config  
Content-Type: application/json
{
  "updates": {
    "MAX_CALLS_PER_HOUR": "5",
    "ACTIVE_HOURS_LOCAL": "09:00-17:00"
  }
}
# Returns: {"ok": true, "saved": ["MAX_CALLS_PER_HOUR", "ACTIVE_HOURS_LOCAL"]}
```

### Greeting Phrase Endpoint
```bash
POST /api/scamcalls/next-greeting
Content-Type: application/json
{
  "phrase": "Hello friend I am calling about your vehicle warranty"
}
# Returns: {"ok": true} or {"ok": false, "error": "Phrase must be 5-15 words"}
```

### Enhanced Call Endpoint
```bash
POST /api/scamcalls/call-now
# Returns: 
# - 200 {"ok": true} - Call initiated
# - 429 {"error": "cap", "message": "Max calls reached..."} - Rate limited
```

## Setup and Configuration

### Environment Variables
Create a `.env` file with required configuration:

```bash
# Required
DEST_NUMBER=+15551234567
FROM_NUMBER=+15557654321

# Optional - Rate Limiting  
MAX_CALLS_PER_HOUR=3
DAILY_MAX_ATTEMPTS_PER_DEST=12

# Optional - Active Window
ACTIVE_HOURS_LOCAL=09:00-18:00
ACTIVE_DAYS=Mon,Tue,Wed,Thu,Fri

# Optional - Security
SECRET_KEY=your-secret-key-for-sessions

# Twilio (if using real calls)
TWILIO_ACCOUNT_SID=your_account_sid
TWILIO_AUTH_TOKEN=your_auth_token
```

### Dependencies
The application includes dependencies in the `vendor/` directory:
- Flask with WebSocket support (flask-sock)
- Twilio SDK (if available)
- Python standard libraries

### Running the Application
```bash
PYTHONPATH=vendor:$PYTHONPATH python3 twilio_outbound_call.py
```

## Manual Testing

### Admin Functionality Test
1. Navigate to `/scamcalls`
2. Click "Admin" button in header
3. Login with `bootycall` / `scammers`
4. Verify admin state shows "Admin (Logged In)"
5. Click again to open Admin Settings panel
6. Modify a safe environment variable (e.g., `MAX_CALLS_PER_HOUR`)
7. Click "Save Changes"
8. Verify changes are reflected and persisted

### Greeting Phrase Test
1. Click "Add greeting phrase" button
2. Enter a 6-10 word custom greeting
3. Verify word count validation
4. Submit phrase
5. Trigger "Call now"
6. Verify custom phrase is used in call flow (replaces default greeting)
7. Confirm phrase is cleared after use

### Rate Limiting Test
1. Configure low `MAX_CALLS_PER_HOUR` value via Admin Settings
2. Trigger multiple calls to hit the limit
3. Verify 429 response and exact toast message appears
4. Confirm no navigation occurs (user stays on page)

### Matrix Background Test
1. Observe cascading 0s and 1s animation in background
2. Verify animation doesn't interfere with clicking/scrolling
3. Test accessibility: set `prefers-reduced-motion: reduce` in browser
4. Confirm animation stops when motion is reduced

## Security Notes

- **Development Credentials**: Change admin credentials in production
- **Session Security**: Uses secure, httpOnly cookies when available
- **Secret Protection**: Automatically prevents exposure/editing of sensitive environment variables
- **Input Validation**: Server-side validation for all user inputs
- **No Credential Logging**: Admin credentials are never logged

## File Structure

```
├── twilio_outbound_call.py    # Main Flask application
├── templates/
│   └── scamcalls.html         # Main UI template with new modals
├── static/
│   ├── scamcalls.js          # Enhanced with admin/greeting/toast functionality
│   └── matrix.js             # Matrix background animation
├── vendor/                   # Bundled dependencies
└── .env                      # Environment configuration
```

## Environment Variable Restart Requirements

Most configuration changes take effect immediately. However, some changes may require an application restart:

- **Immediate Effect**: Rate limits, active windows, feature toggles
- **May Require Restart**: Core Twilio configuration, server settings

The admin interface will indicate when a restart is recommended for specific changes.