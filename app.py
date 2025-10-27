from flask import Flask, render_template
from flask_socketio import SocketIO
from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    AgentWebSocketEvents,
    SettingsOptions,
    FunctionCallRequest,
    FunctionCallResponse,
    Input,
    Output,
)
import os
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*", path='/socket.io')

# Initialize Google Sheets
def init_google_sheets():
    try:
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
        client = gspread.authorize(creds)
        sheet = client.open('Restaurant Orders').sheet1  # Change 'Restaurant Orders' to your sheet name
        print("✅ Google Sheets connected successfully!")
        return sheet
    except FileNotFoundError:
        print("❌ ERROR: credentials.json file not found!")
        return None
    except gspread.exceptions.SpreadsheetNotFound:
        print("❌ ERROR: Spreadsheet 'Restaurant Orders' not found!")
        print("Make sure:")
        print("1. The sheet exists")
        print("2. You've shared it with the service account email")
        return None
    except Exception as e:
        print(f"❌ ERROR initializing Google Sheets: {e}")
        return None

# Initialize the sheet
gsheet = init_google_sheets()
if gsheet:
    try:
        # Create headers if sheet is empty
        if gsheet.row_count == 0 or gsheet.cell(1, 1).value == '':
            gsheet.append_row(['Order ID', 'Date', 'Order Details'])
            print("✅ Headers created in Google Sheet")
    except Exception as e:
        print(f"❌ Error creating headers: {e}")

# Store conversation data
conversation_data = {
    'messages': [],
    'start_time': None,
    'order_id': None
}

def get_next_order_id():
    """Get the next order ID by checking the last row"""
    try:
        if not gsheet:
            return 1
        
        # Get all values in column A (Order ID)
        all_ids = gsheet.col_values(1)
        
        # If only header or empty, start with 1
        if len(all_ids) <= 1:
            return 1
        
        # Get the last ID and increment
        last_id = all_ids[-1]
        try:
            return int(last_id) + 1
        except:
            return 1
    except Exception as e:
        print(f"Error getting next order ID: {e}")
        return 1

def extract_order_details(messages):
    """Extract only order-related information from conversation"""
    order_items = []
    total_price = ""
    
    for msg in messages:
        if msg['role'] == 'assistant':
            content = msg['content'].lower()
            
            # Check if message contains order confirmation or summary
            if any(keyword in content for keyword in ['order', 'total', 'combo', 'burger', 'fries', 'drink', 'coke', 'pizza', 'sandwich']):
                order_items.append(msg['content'])
            
            # Extract total price
            if '$' in msg['content'] or 'total' in content:
                total_price = msg['content']
    
    # Get the most relevant order summary (usually the last comprehensive message)
    if order_items:
        # Look for confirmation messages
        for item in reversed(order_items):
            if 'confirm' in item.lower() or 'order is' in item.lower():
                return item
        # Otherwise return the last item with price info
        for item in reversed(order_items):
            if '$' in item:
                return item
        # Fallback to last order-related message
        return order_items[-1]
    
    return "No order details captured"

def save_to_sheets():
    """Helper function to save conversation to Google Sheets"""
    print("🔴 Save function called")
    print(f"📊 Attempting to save to Google Sheets...")
    print(f"   Messages collected: {len(conversation_data['messages'])}")
    print(f"   Google Sheet available: {gsheet is not None}")
    
    if not gsheet:
        print("❌ Cannot save: Google Sheets not initialized")
        return False
    
    if not conversation_data['messages']:
        print("⚠️ No messages to save")
        return False
    
    try:
        # Extract order details
        order_details = extract_order_details(conversation_data['messages'])
        
        # Format date as YYYY-MM-DD
        date_only = conversation_data['start_time'].split(' ')[0]
        
        print(f"📝 Saving row to Google Sheets...")
        print(f"   Order ID: {conversation_data['order_id']}")
        print(f"   Date: {date_only}")
        print(f"   Order Details: {order_details[:50]}...")
        
        gsheet.append_row([
            conversation_data['order_id'],
            date_only,
            order_details
        ])
        print("✅ Conversation saved to Google Sheets successfully!")
        return True
    except Exception as e:
        print(f"❌ Error saving to Google Sheets: {e}")
        import traceback
        traceback.print_exc()
        return False

# Initialize Deepgram client
config = DeepgramClientOptions(
    options={
        "keepalive": "true",
        "microphone_record": "true",
        "speaker_playback": "true",
    }
)

deepgram = DeepgramClient(os.getenv("DEEPGRAM_API_KEY", ""), config)
dg_connection = deepgram.agent.websocket.v("1")

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    global conversation_data
    
    # Get next order ID
    order_id = get_next_order_id()
    
    conversation_data = {
        'messages': [],
        'start_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'order_id': order_id
    }
    print(f"🔵 New connection started - Order ID: {order_id} at {conversation_data['start_time']}")
    
    options = SettingsOptions()

    # Configure audio input settings
    options.audio.input = Input(
        encoding="linear16",
        sample_rate=16000  # Match the output sample rate
    )

    # Configure audio output settings
    options.audio.output = Output(
        encoding="linear16",
        sample_rate=16000,
        container="none"
    )

    # LLM provider configuration
    options.agent.think.provider.type = "open_ai"
    options.agent.think.provider.model = "gpt-4o-mini"
    options.agent.think.prompt = (
    "You are an AI voice assistant for a fast-food restaurant. "
    "Your job is to take customer orders, customize ingredients, "
    "add or remove items, and offer combo upgrades. "
    "You must confirm every order, summarize it clearly, and give the total price.\n\n"
    "Guidelines:\n"
    "- Always sound friendly, natural, and conversational.\n"
    "- Keep responses short (1–2 sentences, under 120 characters).\n"
    "- Ask one clear follow-up question at a time (e.g., 'Would you like to make it a combo?').\n"
    "- If unclear, ask for clarification instead of assuming.\n"
    "- Confirm customizations (add/remove ingredients) before finalizing.\n"
    "- Always mention the updated total when the order changes.\n"
    "- Never break character; you are a restaurant ordering assistant.\n\n"
    "Remember: you speak and listen. Respond like a real fast-food order taker."
)


    # Deepgram provider configuration
    options.agent.listen.provider.keyterms = ["hello", "goodbye"]
    options.agent.listen.provider.model = "nova-3"
    options.agent.listen.provider.type = "deepgram"
    options.agent.speak.provider.type = "deepgram"

    # Sets Agent greeting
    options.agent.greeting = "Hello! I'm your fastfoody ordering assistant. How may I help you?"

    # Event handlers
    def on_open(self, open, **kwargs):
        print("Open event received:", open.__dict__)
        socketio.emit('open', {'data': open.__dict__})

    def on_welcome(self, welcome, **kwargs):
        print("Welcome event received:", welcome.__dict__)
        socketio.emit('welcome', {'data': welcome.__dict__})

    def on_conversation_text(self, conversation_text, **kwargs):
        print("Conversation event received:", conversation_text.__dict__)
        
        # Store conversation data
        try:
            role = getattr(conversation_text, 'role', 'unknown')
            content = getattr(conversation_text, 'content', str(conversation_text.__dict__))
            
            conversation_data['messages'].append({
                'role': role,
                'content': content
            })
            print(f"💬 Stored message - Role: {role}, Content: {content[:50]}...")
        except Exception as e:
            print(f"❌ Error storing conversation: {e}")
        
        socketio.emit('conversation', {'data': conversation_text.__dict__})

    def on_agent_thinking(self, agent_thinking, **kwargs):
        print("Thinking event received:", agent_thinking.__dict__)
        socketio.emit('thinking', {'data': agent_thinking.__dict__})

    def on_function_call_request(self, function_call_request: FunctionCallRequest, **kwargs):
        print("Function call event received:", function_call_request.__dict__)
        response = FunctionCallResponse(
            function_call_id=function_call_request.function_call_id,
            output="Function response here"
        )
        dg_connection.send_function_call_response(response)
        socketio.emit('function_call', {'data': function_call_request.__dict__})

    def on_agent_started_speaking(self, agent_started_speaking, **kwargs):
        print("Agent speaking event received:", agent_started_speaking.__dict__)
        socketio.emit('agent_speaking', {'data': agent_started_speaking.__dict__})

    def on_error(self, error, **kwargs):
        print("Error event received:", error.__dict__)
        error_data = {
            'message': str(error),
            'type': error.__class__.__name__,
            'details': error.__dict__
        }
        print("Sending error to client:", error_data)
        socketio.emit('error', {'data': error_data})

    # Register event handlers
    dg_connection.on(AgentWebSocketEvents.Open, on_open)
    dg_connection.on(AgentWebSocketEvents.Welcome, on_welcome)
    dg_connection.on(AgentWebSocketEvents.ConversationText, on_conversation_text)
    dg_connection.on(AgentWebSocketEvents.AgentThinking, on_agent_thinking)
    dg_connection.on(AgentWebSocketEvents.FunctionCallRequest, on_function_call_request)
    dg_connection.on(AgentWebSocketEvents.AgentStartedSpeaking, on_agent_started_speaking)
    dg_connection.on(AgentWebSocketEvents.Error, on_error)

    print("Starting Deepgram connection...")
    if not dg_connection.start(options):
        print("Failed to start Deepgram connection")
        socketio.emit('error', {'data': {'message': 'Failed to start connection'}})
        return
    print("Deepgram connection started successfully")

@socketio.on('audio_data')
def handle_audio_data(data):
    try:
        if dg_connection:
            print("Received audio data:", len(data), "bytes")
            # Convert to bytes if needed
            if isinstance(data, list):
                data = bytes(data)
            dg_connection.send_audio(data)
        else:
            print("No Deepgram connection available")
            socketio.emit('error', {'data': {'message': 'No Deepgram connection available'}})
    except Exception as e:
        print("Error handling audio data:", str(e))
        socketio.emit('error', {'data': {'message': f'Error handling audio data: {str(e)}'}})

@socketio.on('save_conversation')
def handle_save_conversation():
    """Manual save trigger from client"""
    print("💾 Manual save triggered")
    success = save_to_sheets()
    socketio.emit('save_result', {'success': success})

@socketio.on('disconnect')
def handle_disconnect():
    print("🔴 Client disconnected - attempting to save...")
    try:
        dg_connection.finish()
    except Exception as e:
        print(f"Error finishing Deepgram connection: {e}")
    
    # Save conversation to Google Sheets
    save_to_sheets()

if __name__ == '__main__':
    socketio.run(app, debug=True, port=3000, host='0.0.0.0')
