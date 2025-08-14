# Scam Call App

A Twilio-powered outbound calling application with live monitoring, admin management, and Matrix-style background effects.

## Features

### Core Functionality
- Automated outbound calling with configurable intervals
- Live call monitoring with real-time transcript
- Call history tracking and export
- WebSocket-based live audio streaming

### Admin Features
- **Admin Login**: Access admin panel with credentials (username: `bootycall`, password: `scammers`)
- **Environment Configuration**: View and edit non-secret environment variables
- **Hot Restart**: Restart the application without losing state
- **Session Management**: Secure session-based authentication with rate limiting

### Call Management
- **Manual Call Trigger**: "Call now" button with cap enforcement
- **Greeting Phrases**: Set custom greeting phrases for individual calls (5-15 words)
- **Rate Limiting**: Automatic enforcement of hourly/daily call caps
- **Backoff Strategy**: Configurable call spacing and cooldowns

### UI Enhancements
- **Matrix Background**: Animated Matrix-style cascading binary effect
- **Toast Notifications**: User-friendly notifications for actions and errors
- **Responsive Design**: Works across different screen sizes
- **Dark Theme**: Optimized for dark mode with proper contrast

## Environment Variables

### Required
- `TWILIO_ACCOUNT_SID`: Your Twilio Account SID
- `TWILIO_AUTH_TOKEN`: Your Twilio Auth Token
- `TO_NUMBER`: Destination phone number (E.164 format)
- `FROM_NUMBER` or `FROM_NUMBERS`: Source phone number(s)

### Optional Configuration
- `ADMIN_SESSION_SECRET`: Secret key for admin sessions (auto-generated if not set)
- `PUBLIC_BASE_URL`: Public URL for webhooks (required for production)
- `LISTEN_HOST`: Host to bind to (default: 0.0.0.0)
- `LISTEN_PORT`: Port to bind to (default: 5005)

### Call Control
- `MIN_INTERVAL_SECONDS`: Minimum seconds between calls (default: 120)
- `MAX_INTERVAL_SECONDS`: Maximum seconds between calls (default: 420)
- `HOURLY_MAX_ATTEMPTS_PER_DEST`: Max calls per hour per destination (default: 3)
- `DAILY_MAX_ATTEMPTS_PER_DEST`: Max calls per day per destination (default: 12)
- `ACTIVE_HOURS_LOCAL`: Active calling hours (default: 09:00-18:00)
- `ACTIVE_DAYS`: Active calling days (default: Mon,Tue,Wed,Thu,Fri)

### Audio & Recording
- `TTS_VOICE`: Text-to-speech voice (default: man)
- `TTS_LANGUAGE`: TTS language (default: en-US) 
- `ENABLE_RECORDING`: Enable call recording (default: false)
- `RECORDING_CHANNELS`: Recording channels mono/dual (default: mono)

## Installation

1. Install dependencies:
```bash
pip install flask twilio python-dotenv flask-sock simple-websocket bcrypt itsdangerous watchdog
```

2. Create `.env` file with required environment variables

3. Run the application:
```bash
python twilio_outbound_call.py
```

4. Access the web interface at `http://localhost:5005/scamcalls`

## Usage

### Admin Access
1. Click the "Admin" button in the navigation
2. Login with username `bootycall` and password `scammers`
3. Configure environment variables through the admin panel
4. Use "Restart App" to apply changes that require restart

### Setting Greeting Phrases
1. Click "Add greeting phrase" next to the "Call now" button
2. Enter a phrase between 5-15 words
3. The phrase will be used for the next call only

### Manual Calls
- Click "Call now" to immediately attempt a call
- Respects all configured caps and active window restrictions
- Shows notification if caps are reached

## API Endpoints

### Admin Endpoints
- `POST /api/admin/login` - Admin authentication
- `POST /api/admin/logout` - Admin logout
- `GET /api/admin/config` - Get environment variables
- `POST /api/admin/config` - Update environment variables

### Call Management
- `POST /api/scamcalls/call-now` - Trigger manual call
- `POST /api/scamcalls/set-greeting` - Set greeting phrase
- `GET /api/scamcalls/status` - Get application status
- `POST /api/scamcalls/reload-now` - Restart application
- `GET /api/scamcalls/reload-status` - Get restart status

## Security Notes

- Admin sessions use secure HTTP-only cookies
- Rate limiting prevents brute force login attempts (5 attempts per 5 minutes)
- Environment variables with secrets are automatically hidden from admin interface
- Session secrets are auto-generated if not provided

## File Structure

```
├── twilio_outbound_call.py    # Main application
├── templates/
│   ├── scamcalls.html         # Live monitoring interface  
│   └── scamcalls_history.html # Call history interface
├── static/
│   ├── scamcalls.js          # Main JavaScript functionality
│   ├── scamcalls.css         # Main styles
│   ├── js/
│   │   └── matrix.js         # Matrix background effect
│   └── css/
│       └── matrix.css        # Matrix and modal styles
└── .env                      # Environment configuration
```