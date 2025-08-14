# Scam Call Application

A Flask-based application for automated outbound calling with live audio monitoring and administrative controls.

## Features

### Core Functionality
- Automated outbound calls on randomized intervals
- Live audio monitoring via WebSockets
- Real-time call transcription
- Call history and transcript storage
- Configurable calling windows and caps

### New Features

#### Admin Login and Configuration
- Secure admin interface with fixed credentials
- Environment variable editor (excludes secrets automatically)
- Configuration persistence to `.env` file
- Application restart functionality
- Session management with brute force protection

**Admin Credentials:**
- Username: `bootycall`
- Password: `scammers`

#### Greeting Phrase Override
- Set custom greeting phrases for individual calls
- 5-15 word validation
- One-time use (consumed on next call)
- Overrides rotating prompts when set

#### Cap Notification
- Pre-call validation against hourly/daily caps
- Toast notifications when caps are exceeded
- Prevents unnecessary call attempts

#### Matrix-Style Background
- Animated falling binary digits (0s and 1s)
- Canvas-based implementation
- Dark mode contrast optimization
- Non-intrusive design

## Environment Variables

### Required
- `TWILIO_ACCOUNT_SID` - Twilio account SID
- `TWILIO_AUTH_TOKEN` - Twilio auth token
- `TO_NUMBER` - Destination phone number (E.164 format)
- `FROM_NUMBER` or `FROM_NUMBERS` - Source phone number(s)

### Optional Configuration
- `ADMIN_SESSION_SECRET` - Secret key for admin sessions (auto-generated if missing)
- `MIN_INTERVAL_SECONDS` - Minimum time between calls (default: 120)
- `MAX_INTERVAL_SECONDS` - Maximum time between calls (default: 420)
- `HOURLY_MAX_ATTEMPTS_PER_DEST` - Hourly call limit (default: 3)
- `DAILY_MAX_ATTEMPTS_PER_DEST` - Daily call limit (default: 12)
- `ACTIVE_HOURS_LOCAL` - Active calling hours (default: "09:00-18:00")
- `ACTIVE_DAYS` - Active calling days (default: "Mon,Tue,Wed,Thu,Fri")
- `BACKOFF_STRATEGY` - Backoff strategy: "none", "linear", or "exponential"

### Admin Configuration
The admin panel automatically filters environment variables to hide secrets. Variables containing any of these patterns (case-insensitive) are excluded from the editor:
- TOKEN, SECRET, PASSWORD, PASS, AUTH, KEY, SID, AUTHTOKEN, ACCOUNT_SID, AUTH_TOKEN

## Usage

### Basic Operation
1. Set required environment variables
2. Run the application: `python twilio_outbound_call.py`
3. Access the web interface at `http://localhost:5000/scamcalls`

### Admin Features
1. Click the "Admin" button in the web interface
2. Login with the provided credentials
3. Edit environment variables in the Admin Settings panel
4. Save changes and restart the application to apply

### Custom Greeting Phrases
1. Click "Add greeting phrase" on the main interface
2. Enter a 5-15 word phrase
3. The phrase will be used for the next call only

### Matrix Background
The Matrix-style animation runs automatically and can be controlled via browser console:
```javascript
MatrixAnimation.stop();  // Stop animation
MatrixAnimation.start(); // Start animation
```

## API Endpoints

### Admin Endpoints
- `POST /api/admin/login` - Admin authentication
- `POST /api/admin/logout` - Clear admin session
- `GET /api/admin/config` - Get editable environment variables
- `POST /api/admin/config` - Update environment variables

### Call Control
- `POST /api/scamcalls/call-now` - Trigger immediate call (with cap checking)
- `POST /api/scamcalls/set-greeting` - Set custom greeting phrase

### Application Control
- `POST /api/scamcalls/reload-now` - Request application restart
- `GET /api/scamcalls/reload-status` - Check restart status

### Status and History
- `GET /api/scamcalls/status` - Current status (includes nextGreeting)
- `GET /api/scamcalls/active` - Active call transcript
- `GET /api/scamcalls/history` - Call history

## Security Notes

- Admin sessions use secure, HttpOnly cookies
- Brute force protection: 5 failed attempts = 5 minute lockout
- Secret environment variables are automatically hidden from admin interface
- Session secret is auto-generated if `ADMIN_SESSION_SECRET` is not set

## File Structure

```
├── twilio_outbound_call.py    # Main application
├── templates/
│   ├── scamcalls.html         # Live monitoring interface
│   └── scamcalls_history.html # Call history interface
├── static/
│   ├── scamcalls.js          # Frontend JavaScript
│   ├── css/
│   │   └── matrix.css        # Matrix animation styles
│   └── js/
│       └── matrix.js         # Matrix animation logic
└── rotating_iv_prompts.py    # Rotating prompt library
```

## Dependencies

```
pip install twilio flask pyngrok python-dotenv flask-sock simple-websocket
```

## License

[Your license here]