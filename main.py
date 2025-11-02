
import eventlet
eventlet.monkey_patch()

import os
import logging
import time
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from flask_socketio import SocketIO, emit, join_room, leave_room
from datetime import datetime, timedelta
import pytz
import json
import random
import string
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, Unit, Battalion, Company, Conduct, User, Session, ActivityLog

# Configure logging for production
logging.basicConfig(
    level=logging.ERROR,  # Only show errors in production
    format='%(asctime)s - %(levelname)s: %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)

# Create Flask app
app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "dev-secret-key")

# Configure database with optimizations
database_url = os.environ.get("DATABASE_URL")
if not database_url:
    # Fallback to SQLite for development/deployment without PostgreSQL
    database_url = "sqlite:///wbgt_app.db"
    print("DATABASE_URL not found, using SQLite fallback")
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_recycle": 300,
    "pool_pre_ping": True,
    "pool_size": 10,
    "max_overflow": 20,
    "echo": False  # Disable SQL logging for performance
}
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

# Initialize extensions with production-ready settings
socketio = SocketIO(app, async_mode='eventlet', ping_timeout=60, ping_interval=25, 
                    logger=False, engineio_logger=False, cors_allowed_origins="*",
                    transports=['websocket', 'polling'])

# Initialize database on startup
db.init_app(app)

# Initialize database immediately
def init_db():
    with app.app_context():
        # Create tables if they don't exist (first time setup)
        try:
            db.create_all()
            print("Database tables created successfully")
        except Exception as e:
            print(f"Error creating tables: {e}")
        
        # Safe column addition with proper transaction handling
        try:
            # Start fresh transaction
            db.session.rollback()
            
            # Check if most_stringent_zone column exists (PostgreSQL and SQLite compatible)
            try:
                db.session.execute(db.text("SELECT most_stringent_zone FROM \"user\" LIMIT 1"))
                column_exists = True
            except Exception:
                column_exists = False
            
            if not column_exists:
                print("Adding missing most_stringent_zone column to user table")
                # Use proper PostgreSQL syntax for reserved keyword
                db.session.execute(db.text('ALTER TABLE "user" ADD COLUMN most_stringent_zone VARCHAR(20)'))
                db.session.commit()
                print("Successfully added most_stringent_zone column")
            else:
                print("Database schema is up to date - most_stringent_zone column exists")
                
        except Exception as e:
            print(f"Schema check/update error: {e}")
            db.session.rollback()
            # Ensure tables exist even if column addition fails
            try:
                db.create_all()
                print("Ensured all tables exist")
            except Exception as create_error:
                print(f"Error ensuring tables exist: {create_error}")
            
        # One-time migration: Set last_activity_at for existing conducts
        try:
            db.session.rollback()  # Start fresh
            
            conducts_without_activity = Conduct.query.filter(Conduct.last_activity_at.is_(None)).all()
            if conducts_without_activity:
                print(f"Migrating {len(conducts_without_activity)} conducts to add last_activity_at field")
                for conduct in conducts_without_activity:
                    conduct.last_activity_at = conduct.created_at  # Use creation time as initial activity
                db.session.commit()
                print("Migration completed successfully")
        except Exception as e:
            print(f"Migration error: {e}")
            db.session.rollback()

init_db()

# Constants
SG_TZ = pytz.timezone("Asia/Singapore")
WBGT_ZONES = {
    "white": {"work": 60, "rest": 15},
    "green": {"work": 45, "rest": 15},
    "yellow": {"work": 30, "rest": 15},
    "red": {"work": 30, "rest": 30},
    "black": {"work": 15, "rest": 30},
    "test": {"work": 7/60, "rest": 10/60},  # 7 seconds work, 10 seconds rest
    "cut-off": {"work": 0, "rest": 30}
}

# Zone stringency hierarchy (most stringent = highest index)
ZONE_STRINGENCY = {
    "white": 0,
    "green": 1, 
    "yellow": 2,
    "red": 3,
    "black": 4,
    "cut-off": 5,
    "test": 6  # Most stringent for testing purposes
}

# Global system status for each conduct
conduct_system_status = {}

# Simple cache for user data to reduce database queries
user_cache = {}
CACHE_TIMEOUT = 30  # seconds

# Background task control
background_task_started = False

def get_cached_user(user_id):
    """Get user from cache or database"""
    current_time = datetime.now().timestamp()

    if user_id in user_cache:
        cached_data, timestamp = user_cache[user_id]
        if current_time - timestamp < CACHE_TIMEOUT:
            return cached_data

    # Cache miss or expired, fetch from database
    user = User.query.get(user_id)
    if user:
        user_cache[user_id] = (user, current_time)

    return user

def invalidate_user_cache(user_id):
    """Remove user from cache when updated"""
    if user_id in user_cache:
        del user_cache[user_id]

def get_conduct_system_status(conduct_id):
    """Get system status for a specific conduct"""
    if conduct_id not in conduct_system_status:
        conduct_system_status[conduct_id] = {
            "cut_off": False,
            "cut_off_end_time": None
        }
    return conduct_system_status[conduct_id]

def sg_now():
    """Get current Singapore time as naive datetime"""
    now = datetime.now(SG_TZ)
    # Return naive datetime (without timezone info) in Singapore time
    return now.replace(tzinfo=None, microsecond=0)

def get_most_stringent_zone(current_zone, previous_most_stringent):
    """Determine the most stringent zone between current and previous"""
    if not previous_most_stringent:
        return current_zone
    
    current_stringency = ZONE_STRINGENCY.get(current_zone, 0)
    previous_stringency = ZONE_STRINGENCY.get(previous_most_stringent, 0)
    
    # Return the more stringent zone (higher index = more stringent)
    if current_stringency >= previous_stringency:
        return current_zone
    else:
        return previous_most_stringent

def get_rest_duration_for_most_stringent_zone(most_stringent_zone):
    """Get rest duration based on most stringent zone experienced during cycle"""
    if not most_stringent_zone:
        return 15  # Default to white zone rest
    return WBGT_ZONES.get(most_stringent_zone, {}).get('rest', 15)

def emit_user_update(conduct_id, user):
    """Emit user update to all clients in conduct room"""
    try:
        socketio.emit('user_update', {
            'user': user.name,
            'status': user.status,
            'zone': user.zone,
            'start_time': user.start_time,
            'end_time': user.end_time,
            'work_completed': user.work_completed,
            'pending_rest': user.pending_rest,
            'role': user.role
        }, room=f'conduct_{conduct_id}')
        print(f"Emitted user update for {user.name} to conduct room {conduct_id}")
    except Exception as e:
        logging.error(f"Error emitting user update: {e}")

def emit_system_status_update(conduct_id, system_status):
    """Emit system status update to all clients in conduct room"""
    try:
        socketio.emit('system_status_update', system_status, room=f'conduct_{conduct_id}')
        print(f"Emitted system status update to conduct room {conduct_id}: {system_status}")
    except Exception as e:
        logging.error(f"Error emitting system status update: {e}")

def log_activity(conduct_id, username, action, zone=None, details=None):
    """Log activity for a specific conduct"""
    try:
        # Get current Singapore time
        singapore_time = sg_now()
        
        # Enhanced details based on action type with join/rest times
        if action == 'user_joined' and not details:
            details = f"User joined conduct at {singapore_time.strftime('%I:%M:%S %p')}"
        elif action == 'start_rest' and not details:
            details = f"Started rest period for {zone} zone at {singapore_time.strftime('%I:%M:%S %p')}"
        elif action == 'completed_rest' and not details:
            details = f"Completed rest period for {zone} zone at {singapore_time.strftime('%I:%M:%S %p')}"
        elif action == 'completed_work' and not details:
            details = f"Completed work cycle for {zone} zone at {singapore_time.strftime('%I:%M:%S %p')}"
        elif action == 'early_completion' and not details:
            details = f"Cycle ended early by user at {singapore_time.strftime('%I:%M:%S %p')}"
        elif action == 'interface_reset' and not details:
            details = f"Trainer interface reset at {singapore_time.strftime('%I:%M:%S %p')}"

        print(f"DEBUG: Creating activity log - conduct_id: {conduct_id}, username: {username}, action: {action}, zone: {zone}, details: {details}")

        activity = ActivityLog(
            conduct_id=conduct_id,
            username=username,
            action=action,
            zone=zone,
            details=details,
            timestamp=singapore_time
        )
        db.session.add(activity)
        
        # Flush to ensure the record is written to the database
        db.session.flush()
        print(f"DEBUG: Activity log flushed to database for {username}: {action}")
        
        db.session.commit()
        print(f"DEBUG: Activity log committed to database for {username}: {action}")

        # Emit to conduct room (unlimited history)
        socketio.emit('history_update', {
            'history': get_recent_history(conduct_id)
        }, room=f'conduct_{conduct_id}')
        print(f"DEBUG: History update emitted for conduct {conduct_id}")

    except Exception as e:
        print(f"ERROR logging activity: {e}")
        logging.error(f"Error logging activity: {e}")
        db.session.rollback()

def get_recent_history(conduct_id, limit=None):
    """Get activity history for a conduct (unlimited by default)"""
    query = ActivityLog.query.filter_by(conduct_id=conduct_id)\
                           .order_by(ActivityLog.timestamp.desc())
    
    # Only apply limit if specified
    if limit:
        query = query.limit(limit)
    
    logs = query.all()

    return [{
        'timestamp': log.timestamp.strftime('%Y-%m-%d %I:%M:%S %p'),
        'username': log.username,
        'action': log.action,
        'zone': log.zone,
        'details': log.details
    } for log in logs]

def show_work_complete_modal(username, zone):
    """Send work complete modal notification to specific user"""
    rest_duration = WBGT_ZONES.get(zone, {}).get('rest', 15)

    # Create notification data
    notification_data = {
        'username': username,
        'zone': zone,
        'rest_duration': rest_duration,
        'title': 'Work Cycle Complete!',
        'message': 'Your work cycle has ended. Time to start rest cycle!'
    }

    # Emit to specific user
    socketio.emit('show_work_complete_modal', notification_data)

    print(f"Work complete modal shown for {username} in {zone} zone")
    return True

def check_conduct_activity():
    """Background task to check for conducts that should be deactivated after 24 hours with no users"""
    with app.app_context():
        try:
            now = sg_now()
            # Calculate 24 hours ago
            twenty_four_hours_ago = now - timedelta(hours=24)
            
            # Find active conducts with no activity for more than 24 hours
            old_conducts = Conduct.query.filter(
                Conduct.status == 'active',
                Conduct.last_activity_at < twenty_four_hours_ago
            ).all()
            
            for conduct in old_conducts:
                # Check if there are any currently active users in this conduct
                active_user_count = User.query.filter_by(conduct_id=conduct.id).filter(
                    User.status.in_(['working', 'resting'])
                ).count()
                
                if active_user_count == 0:
                    # No active users in this conduct for 24 hours - deactivate it
                    conduct.status = 'inactive'
                    print(f"Conduct '{conduct.name}' (PIN: {conduct.pin}) automatically deactivated after 24 hours with no active users")
                    
                    # Log the deactivation activity if there's activity logging
                    try:
                        log_activity(conduct.id, "SYSTEM", 'conduct_deactivated', 
                                   details=f"Conduct automatically deactivated after 24 hours with no active users at {now.strftime('%Y-%m-%d %H:%M:%S')}")
                    except:
                        # If logging fails, continue with deactivation
                        pass
            
            # Commit all changes
            if old_conducts:
                db.session.commit()
                
        except Exception as e:
            print(f"Error in conduct activity check: {e}")
            logging.error(f"Error in conduct activity check: {e}")
            db.session.rollback()

# Background task to check for work cycle completions
def check_user_cycles():
    """Background task to check for completed work cycles and trigger notifications"""
    with app.app_context():  # CRITICAL: Add application context
        try:
            now = sg_now()
            current_time_str = now.strftime('%H:%M:%S')

            # Find users whose work cycles should have ended
            working_users = User.query.filter_by(status='working').all()

            for user in working_users:
                if user.end_time:
                    # Parse end time
                    end_time_parts = user.end_time.split(':')
                    end_time = now.replace(
                        hour=int(end_time_parts[0]),
                        minute=int(end_time_parts[1]),
                        second=int(end_time_parts[2]) if len(end_time_parts) > 2 else 0,
                        microsecond=0
                    )
                    
                    # Handle midnight rollover: if end time is earlier than start time,
                    # it means the end time is on the next day
                    if user.start_time:
                        start_time_parts = user.start_time.split(':')
                        start_hour = int(start_time_parts[0])
                        end_hour = int(end_time_parts[0])
                        
                        # If end time is significantly earlier than start time, assume next day
                        if end_hour < start_hour and (start_hour - end_hour) > 12:
                            end_time = end_time + timedelta(days=1)
                            if not hasattr(user, '_midnight_logged') or not user._midnight_logged:
                                print(f"Server: Midnight rollover detected for working user {user.name}: {user.end_time} moved to next day")
                                user._midnight_logged = True

                    # If work cycle has ended
                    if now >= end_time and not user.work_completed:
                        print(f"Work cycle completed for user {user.name} in zone {user.zone}")

                        # Mark work as completed and pending rest
                        user.work_completed = True
                        user.pending_rest = True
                        user.status = 'idle'  # Change status but keep other data for notification

                        db.session.commit()
                        invalidate_user_cache(user.id)

                        # Log completion
                        log_activity(user.conduct_id, user.name, 'completed_work', user.zone, 
                                    f"Work cycle completed automatically at {current_time_str}")

                        # Emit user update
                        emit_user_update(user.conduct_id, user)

                        # Show work complete modal
                        show_work_complete_modal(user.name, user.zone)

                        # Also emit work cycle completed event for enhanced notification handling
                        socketio.emit('work_cycle_completed', {
                            'username': user.name,
                            'zone': user.zone,
                            'rest_time': WBGT_ZONES.get(user.zone, {}).get('rest', 15),
                            'action': 'work_cycle_completed'
                        }, room=f'conduct_{user.conduct_id}')

                        print(f"Work completion notification sent for {user.name}")

            # Check for resting users
            resting_users = User.query.filter_by(status='resting').all()

            for user in resting_users:
                if user.end_time:
                    # Parse end time
                    end_time_parts = user.end_time.split(':')
                    end_time = now.replace(
                        hour=int(end_time_parts[0]),
                        minute=int(end_time_parts[1]),
                        second=int(end_time_parts[2]) if len(end_time_parts) > 2 else 0,
                        microsecond=0
                    )
                    
                    # Handle midnight rollover: if end time is earlier than start time,
                    # it means the end time is on the next day
                    if user.start_time:
                        start_time_parts = user.start_time.split(':')
                        start_hour = int(start_time_parts[0])
                        end_hour = int(end_time_parts[0])
                        
                        # If end time is significantly earlier than start time, assume next day
                        if end_hour < start_hour and (start_hour - end_hour) > 12:
                            end_time = end_time + timedelta(days=1)
                            print(f"Server: Midnight rollover detected for resting user {user.name}: {user.end_time} moved to next day")

                    # Calculate exact time difference
                    time_diff = (end_time - now).total_seconds()
                    
                    # If rest cycle has ended (use exact timing)
                    if time_diff <= 0:
                        print(f"Rest cycle completed for user {user.name} (time diff: {time_diff:.1f}s)")
                        print(f"TIMING DEBUG: Completion - Start: {user.start_time}, End: {user.end_time}, Actual: {now.strftime('%H:%M:%S')}")

                        # Store zone and conduct info before clearing for logging
                        completed_zone = user.zone
                        conduct_id = user.conduct_id
                        user_name = user.name
                        
                        # Use the intended end time for accurate logging (the stored end_time)
                        intended_end_time = user.end_time  # Use the stored end time string directly

                        # CRITICAL FIX: Log completion BEFORE resetting user data
                        # This ensures the zone information is available for logging
                        try:
                            # RENDER PRODUCTION FIX: Create activity log in separate transaction
                            activity_log = ActivityLog(
                                conduct_id=conduct_id,
                                username=user_name,
                                action='completed_rest',
                                zone=completed_zone,
                                details=f"Rest cycle completed automatically at {intended_end_time}",
                                timestamp=end_time  # Use calculated end_time for precise duration
                            )
                            
                            db.session.add(activity_log)
                            db.session.flush()  # Flush to get the log ID
                            print(f"RENDER DEBUG: Activity log created for {user_name}: completed_rest in {completed_zone}")
                            
                            # Reset user data - ENSURE all flags are cleared
                            user.status = 'idle'
                            user.zone = None
                            user.start_time = None
                            user.end_time = None
                            user.work_completed = False
                            user.pending_rest = False
                            user.most_stringent_zone = None  # Reset stringent zone tracker
                            
                            # Commit BOTH activity log and user changes together
                            db.session.commit()
                            print(f"RENDER DEBUG: Combined database commit successful for {user_name} rest completion")
                            
                            # CRITICAL: Add a small delay to ensure database transaction is fully completed
                            import time
                            time.sleep(0.2)  # 200ms delay to ensure database consistency on Render
                            
                            # Verify the activity log was actually saved
                            verification_log = ActivityLog.query.filter_by(
                                conduct_id=conduct_id,
                                username=user_name,
                                action='completed_rest'
                            ).order_by(ActivityLog.timestamp.desc()).first()
                            
                            if verification_log:
                                print(f"RENDER DEBUG: Activity log verified in database - ID: {verification_log.id}, Time: {verification_log.timestamp}")
                            else:
                                print(f"RENDER DEBUG: WARNING - Activity log not found in database after commit")
                            
                        except Exception as combined_error:
                            print(f"RENDER DEBUG: Error in combined transaction: {combined_error}")
                            db.session.rollback()
                            
                            # Fallback: Try logging activity separately
                            try:
                                fallback_log = ActivityLog(
                                    conduct_id=conduct_id,
                                    username=user_name,
                                    action='completed_rest',
                                    zone=completed_zone,
                                    details=f"Rest cycle completed automatically at {intended_end_time} (fallback)",
                                    timestamp=end_time  # Use intended end time for accuracy
                                )
                                db.session.add(fallback_log)
                                db.session.commit()
                                print(f"RENDER DEBUG: Fallback activity log successful for {user_name}")
                                
                                # Now update user separately
                                user.status = 'idle'
                                user.zone = None
                                user.start_time = None
                                user.end_time = None
                                user.work_completed = False
                                user.pending_rest = False
                                user.most_stringent_zone = None  # Reset stringent zone tracker
                                db.session.commit()
                                print(f"RENDER DEBUG: Fallback user update successful for {user_name}")
                                
                            except Exception as fallback_error:
                                print(f"RENDER DEBUG: Fallback also failed: {fallback_error}")
                                db.session.rollback()

                        invalidate_user_cache(user.id)

                        # Emit user update
                        emit_user_update(conduct_id, user)

                        # Emit specific event for zone button re-enabling
                        socketio.emit('rest_cycle_completed', {
                            'user': user_name,
                            'zone': completed_zone,
                            'action': 'rest_cycle_completed'
                        }, room=f'conduct_{conduct_id}')

                        # Force immediate history refresh for monitors - ENHANCED VERSION
                        try:
                            # Small delay before emitting to ensure database is fully consistent
                            time.sleep(0.1)
                            
                            updated_history = get_recent_history(conduct_id)
                            
                            # Emit history update with complete data
                            socketio.emit('history_update', {
                                'history': updated_history,
                                'conduct_id': conduct_id,
                                'trigger': 'rest_completion'
                            }, room=f'conduct_{conduct_id}')
                            print(f"RENDER DEBUG: History update emitted with {len(updated_history)} entries")
                            
                            # Also emit a global history refresh to ensure all monitors update
                            socketio.emit('force_history_refresh', {
                                'conduct_id': conduct_id,
                                'message': f'{user_name} completed rest cycle in {completed_zone} zone',
                                'action': 'rest_completed'
                            }, room=f'conduct_{conduct_id}')
                            print(f"RENDER DEBUG: Force history refresh emitted for {user_name}")
                            
                        except Exception as history_error:
                            print(f"RENDER DEBUG: Error emitting history update: {history_error}")
                            # Fallback: Try again with simpler data
                            try:
                                socketio.emit('force_history_refresh', {
                                    'conduct_id': conduct_id,
                                    'fallback': True
                                }, room=f'conduct_{conduct_id}')
                            except:
                                pass

                        print(f"Rest completion processed successfully for {user_name} in zone {completed_zone}")

        except Exception as e:
            logging.error(f"Error in work completion check: {e}")

# Routes (keeping all existing routes unchanged...)

@app.route('/')
def index():
    """Page 1: Home - Conduct Access & Management"""
    return render_template('index_new.html')

# New Battalion-Company Structure Routes

@app.route('/create_conduct_new', methods=['GET', 'POST'])
def create_conduct_new():
    if request.method == 'POST':
        battalion_name = request.form.get('battalion_name', '').strip()
        company_name = request.form.get('company_name', '').strip()
        conduct_name = request.form.get('conduct_name', '').strip()
        company_password = request.form.get('company_password', '').strip()

        if not all([battalion_name, company_name, conduct_name, company_password]):
            flash('All fields are required.', 'error')
            return render_template('create_conduct_new.html')

        try:
            # Check if battalion exists (case insensitive)
            battalion = Battalion.query.filter(Battalion.name.ilike(battalion_name)).first()
            if not battalion:
                # Create new battalion
                battalion = Battalion(name=battalion_name)
                battalion.set_password('test123')  # Default password for HQ access
                db.session.add(battalion)
                db.session.flush()  # Get battalion ID

            # Check if company exists in this battalion (case insensitive)
            company = Company.query.filter(
                Company.battalion_id == battalion.id,
                Company.name.ilike(company_name)
            ).first()

            if company:
                # Verify company password
                if not company.check_password(company_password):
                    flash('Incorrect company password.', 'error')
                    return render_template('create_conduct_new.html')
            else:
                # Create new company
                company = Company(
                    battalion_id=battalion.id,
                    name=company_name
                )
                company.set_password(company_password)
                db.session.add(company)
                db.session.flush()  # Get company ID

            # Create new conduct
            conduct = Conduct(
                company_id=company.id,
                name=conduct_name
            )
            conduct.generate_pin()
            db.session.add(conduct)
            db.session.commit()

            flash(f'Conduct "{conduct_name}" created successfully! PIN: {conduct.pin}', 'success')
            return redirect(url_for('company_conducts', company_id=company.id))

        except Exception as e:
            db.session.rollback()
            flash(f'Error creating conduct: {str(e)}', 'error')
            return render_template('create_conduct_new.html')

    return render_template('create_conduct_new.html')

@app.route('/view_conducts_new', methods=['GET', 'POST'])
def view_conducts_new():
    if request.method == 'POST':
        access_type = request.form.get('access_type', '').strip()
        
        if access_type == 'battalion':
            # Battalion HQ access
            battalion_name = request.form.get('battalion_name', '').strip()
            battalion_password = request.form.get('battalion_password', '').strip()
            
            if not all([battalion_name, battalion_password]):
                flash('Both battalion name and password are required.', 'error')
                return render_template('view_conducts_new.html')
            
            try:
                battalion = Battalion.query.filter(Battalion.name.ilike(battalion_name)).first()
                if battalion and battalion.check_password(battalion_password):
                    return redirect(url_for('battalion_overview', battalion_id=battalion.id))
                else:
                    flash('Invalid battalion name or password.', 'error')
                    return render_template('view_conducts_new.html')
                    
            except Exception as e:
                flash(f'Error accessing battalion: {str(e)}', 'error')
                return render_template('view_conducts_new.html')
                
        elif access_type == 'company':
            # Company access with battalion specification
            company_battalion_name = request.form.get('company_battalion_name', '').strip()
            company_name = request.form.get('company_name', '').strip()
            company_password = request.form.get('company_password', '').strip()
            
            if not all([company_battalion_name, company_name, company_password]):
                flash('Battalion name, company name, and company password are all required.', 'error')
                return render_template('view_conducts_new.html')
            
            try:
                # First find the battalion
                battalion = Battalion.query.filter(Battalion.name.ilike(company_battalion_name)).first()
                if not battalion:
                    flash('Battalion not found. Please check the battalion name.', 'error')
                    return render_template('view_conducts_new.html')
                
                # Then find the company within that battalion
                company = Company.query.filter(
                    Company.battalion_id == battalion.id,
                    Company.name.ilike(company_name)
                ).first()
                
                if company and company.check_password(company_password):
                    return redirect(url_for('company_conducts', company_id=company.id))
                else:
                    flash('Invalid company name or password for the specified battalion.', 'error')
                    return render_template('view_conducts_new.html')
                    
            except Exception as e:
                flash(f'Error accessing company: {str(e)}', 'error')
                return render_template('view_conducts_new.html')
        else:
            flash('Invalid access type selected.', 'error')
            return render_template('view_conducts_new.html')

    return render_template('view_conducts_new.html')

@app.route('/battalion_overview/<int:battalion_id>')
def battalion_overview(battalion_id):
    try:
        battalion = Battalion.query.get_or_404(battalion_id)
        companies = Company.query.filter_by(battalion_id=battalion_id).order_by(Company.name).all()
        
        # Calculate statistics
        total_conducts = 0
        active_conducts = 0
        
        for company in companies:
            total_conducts += len(company.conducts)
            active_conducts += len([c for c in company.conducts if c.status == 'active'])

        return render_template('battalion_overview.html',
                             battalion=battalion,
                             company_data=companies,
                             total_conducts=total_conducts,
                             active_conducts=active_conducts)
    except Exception as e:
        flash(f'Error loading battalion overview: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/company_conducts/<int:company_id>')
def company_conducts(company_id):
    try:
        company = Company.query.get_or_404(company_id)
        conducts = Conduct.query.filter_by(company_id=company_id).order_by(Conduct.created_at.desc()).all()
        
        return render_template('company_conducts.html',
                             company=company,
                             conducts=conducts)
    except Exception as e:
        flash(f'Error loading company conducts: {str(e)}', 'error')
        return redirect(url_for('index'))

@app.route('/delete_conducts/<int:battalion_id>', methods=['POST'])
def delete_conducts(battalion_id):
    """Delete selected conducts - only accessible by battalion accounts"""
    try:
        # Verify battalion access
        battalion = Battalion.query.get_or_404(battalion_id)
        
        # Get list of conduct IDs to delete
        conduct_ids = request.form.getlist('conduct_ids')
        if not conduct_ids:
            flash('No conducts selected for deletion.', 'error')
            return redirect(url_for('battalion_overview', battalion_id=battalion_id))
        
        # Convert to integers and validate
        try:
            conduct_ids = [int(cid) for cid in conduct_ids]
        except ValueError:
            flash('Invalid conduct IDs provided.', 'error')
            return redirect(url_for('battalion_overview', battalion_id=battalion_id))
        
        # Verify all conducts belong to companies in this battalion
        conducts_to_delete = []
        for conduct_id in conduct_ids:
            conduct = Conduct.query.get(conduct_id)
            if not conduct:
                flash(f'Conduct ID {conduct_id} not found.', 'error')
                return redirect(url_for('battalion_overview', battalion_id=battalion_id))
            
            # Check if conduct belongs to a company in this battalion
            if conduct.company.battalion_id != battalion_id:
                flash(f'Conduct "{conduct.name}" does not belong to this battalion.', 'error')
                return redirect(url_for('battalion_overview', battalion_id=battalion_id))
            
            conducts_to_delete.append(conduct)
        
        # Delete associated records first (sessions, users, activity logs)
        deleted_count = 0
        for conduct in conducts_to_delete:
            # Delete sessions first
            Session.query.filter_by(conduct_id=conduct.id).delete()
            
            # Delete activity logs
            ActivityLog.query.filter_by(conduct_id=conduct.id).delete()
            
            # Delete users
            User.query.filter_by(conduct_id=conduct.id).delete()
            
            # Delete the conduct itself
            db.session.delete(conduct)
            deleted_count += 1
        
        # Commit all deletions
        db.session.commit()
        
        flash(f'Successfully deleted {deleted_count} conduct(s).', 'success')
        return redirect(url_for('battalion_overview', battalion_id=battalion_id))
        
    except Exception as e:
        db.session.rollback()
        flash(f'Error deleting conducts: {str(e)}', 'error')
        return redirect(url_for('battalion_overview', battalion_id=battalion_id))

@app.route('/create_conduct', methods=['GET', 'POST'])
def create_conduct():
    """Page 2A: Create Conduct with Authentication"""
    if request.method == 'POST':
        unit_name = request.form.get('unit_name', '').strip()
        conduct_name = request.form.get('conduct_name', '').strip()
        unit_password = request.form.get('unit_password', '').strip()

        if not all([unit_name, conduct_name, unit_password]):
            flash('All fields are required', 'error')
            return render_template('create_conduct.html')

        try:
            # Check if unit exists
            unit = Unit.query.filter_by(name=unit_name).first()

            if unit:
                # Existing unit - verify password
                if not unit.check_password(unit_password):
                    flash('Invalid password for this unit', 'error')
                    return render_template('create_conduct.html')
            else:
                # New unit - create it
                unit = Unit(name=unit_name)
                unit.set_password(unit_password)
                db.session.add(unit)
                db.session.flush()  # Get the ID

            # Create conduct
            conduct = Conduct(
                unit_id=unit.id,
                name=conduct_name
            )
            conduct.generate_pin()
            db.session.add(conduct)
            db.session.commit()

            flash(f'Conduct created successfully! PIN: {conduct.pin}', 'success')
            return redirect(url_for('conduct_list', unit_id=unit.id))

        except Exception as e:
            db.session.rollback()
            logging.error(f"Error creating conduct: {e}")
            flash('Error creating conduct', 'error')

    return render_template('create_conduct.html')

@app.route('/view_conducts', methods=['GET', 'POST'])
def view_conducts():
    """Page 2B: Authenticate to View Conduct List"""
    if request.method == 'POST':
        unit_name = request.form.get('unit_name', '').strip()
        unit_password = request.form.get('unit_password', '').strip()

        if not all([unit_name, unit_password]):
            flash('All fields are required', 'error')
            return render_template('view_conducts.html')

        unit = Unit.query.filter_by(name=unit_name).first()

        if not unit or not unit.check_password(unit_password):
            flash('Invalid unit name or password', 'error')
            return render_template('view_conducts.html')

        return redirect(url_for('conduct_list', unit_id=unit.id))

    return render_template('view_conducts.html')

@app.route('/conduct_list/<int:unit_id>')
def conduct_list(unit_id):
    """Page 3: Conduct List Dashboard"""
    unit = Unit.query.get_or_404(unit_id)
    conducts = Conduct.query.filter_by(unit_id=unit_id).order_by(Conduct.created_at.desc()).all()

    return render_template('conduct_list.html', unit=unit, conducts=conducts)

@app.route('/join_conduct', methods=['GET', 'POST'])
def join_conduct():
    """Page 4: Conduct Join Verification"""
    if request.method == 'POST':
        pin = request.form.get('pin', '').strip()

        if not pin or len(pin) != 6:
            flash('Please enter a valid 6-digit PIN', 'error')
            return render_template('join_conduct.html')

        conduct = Conduct.query.filter_by(pin=pin).first()

        if not conduct:
            flash('Invalid PIN - conduct not found', 'error')
            return render_template('join_conduct.html')
            
        # If conduct is inactive, reactivate it when someone joins
        if conduct.status == 'inactive':
            conduct.status = 'active'
            conduct.last_activity_at = sg_now()  # Reset the 24-hour timer
            db.session.commit()
            print(f"Conduct '{conduct.name}' (PIN: {conduct.pin}) reactivated by user joining")
            
            # Log the reactivation
            try:
                log_activity(conduct.id, "SYSTEM", 'conduct_reactivated', 
                           details=f"Conduct reactivated by user joining at {sg_now().strftime('%Y-%m-%d %H:%M:%S')}")
            except:
                pass

        return redirect(url_for('user_setup', conduct_id=conduct.id))

    return render_template('join_conduct.html')

@app.route('/user_setup/<int:conduct_id>', methods=['GET', 'POST'])
def user_setup(conduct_id):
    """Page 5: Identity & Role Selection"""
    conduct = Conduct.query.get_or_404(conduct_id)
    
    # If conduct is inactive, reactivate it when someone joins
    if conduct.status == 'inactive':
        conduct.status = 'active'
        conduct.last_activity_at = sg_now()  # Reset the 24-hour timer
        db.session.commit()
        print(f"Conduct '{conduct.name}' (PIN: {conduct.pin}) reactivated by user accessing setup")
        
        # Log the reactivation
        try:
            log_activity(conduct.id, "SYSTEM", 'conduct_reactivated', 
                       details=f"Conduct reactivated by user accessing setup at {sg_now().strftime('%Y-%m-%d %H:%M:%S')}")
        except:
            pass

    if request.method == 'POST':
        user_name = request.form.get('user_name', '').strip()
        role = request.form.get('role', '').strip()
        conducting_body_password = request.form.get('conducting_body_password', '').strip()

        if not all([user_name, role]):
            flash('Name and role are required', 'error')
            return render_template('user_setup.html', conduct=conduct)

        # Validate conducting body password
        if role == 'conducting_body':
            if conducting_body_password != 'password':  # Default password
                flash('Invalid conducting body password', 'error')
                return render_template('user_setup.html', conduct=conduct)

        try:
            # Check if user already exists in this conduct
            existing_user = User.query.filter_by(name=user_name, conduct_id=conduct_id).first()

            if existing_user:
                # Update existing user
                existing_user.role = role
                existing_user.status = 'idle' if role == 'trainer' else 'monitoring'
                db.session.commit()
                user = existing_user
            else:
                # Create new user
                user = User(
                    name=user_name,
                    role=role,
                    conduct_id=conduct_id,
                    status='idle' if role == 'trainer' else 'monitoring'
                )
                db.session.add(user)
                db.session.commit()

            # Update conduct activity when user joins
            conduct.last_activity_at = sg_now()
            db.session.commit()

            # Store in session
            session['user_id'] = user.id
            session['conduct_id'] = conduct_id

            # Emit user update for real-time monitoring (for trainers only)
            if role == 'trainer':
                emit_user_update(conduct_id, user)
                # Also log the join activity
                log_activity(conduct_id, user.name, 'user_joined', details=f"Trainer {user.name} joined the conduct")

            # Redirect based on role
            if role == 'trainer':
                return redirect(url_for('dashboard', user_id=user.id))
            else:
                return redirect(url_for('monitor', user_id=user.id))

        except Exception as e:
            db.session.rollback()
            logging.error(f"Error setting up user: {e}")
            flash('Error setting up user', 'error')

    return render_template('user_setup.html', conduct=conduct)

@app.route('/dashboard/<int:user_id>')
def dashboard(user_id):
    """Page 6: Trainer Interface"""
    user = get_cached_user(user_id)
    if not user:
        return redirect(url_for('index'))

    if user.role != 'trainer':
        return redirect(url_for('index'))

    system_status = get_conduct_system_status(user.conduct_id)

    # Only log initial join, not page refreshes
    # Check if this is a fresh session by looking at recent activity
    recent_activity = ActivityLog.query.filter_by(
        conduct_id=user.conduct_id,
        username=user.name,
        action='user_joined'
    ).filter(ActivityLog.timestamp >= sg_now() - timedelta(minutes=5)).first()
    
    # Only log if no recent join activity (avoid logging every page refresh)
    if not recent_activity:
        log_activity(user.conduct_id, user.name, 'user_joined', details=f"Trainer accessed dashboard")

    return render_template('dashboard.html', 
                         user=user, 
                         username=user.name,
                         zones=WBGT_ZONES,
                         system_status=system_status)

@app.route('/monitor/<int:user_id>')
def monitor(user_id):
    """Page 7: Conducting Body Interface"""
    user = get_cached_user(user_id)
    if not user:
        return redirect(url_for('index'))

    if user.role != 'conducting_body':
        return redirect(url_for('index'))

    # Get all users in this conduct
    users = User.query.filter_by(conduct_id=user.conduct_id).all()

    users_dict = {u.name: {
        'role': u.role,
        'status': u.status,
        'zone': u.zone,
        'start_time': u.start_time,
        'end_time': u.end_time,
        'location': u.location,
        'work_completed': u.work_completed,
        'pending_rest': u.pending_rest
    } for u in users}

    system_status = get_conduct_system_status(user.conduct_id)

    # Only log initial join, not page refreshes
    # Check if this is a fresh session by looking at recent activity
    recent_activity = ActivityLog.query.filter_by(
        conduct_id=user.conduct_id,
        username=user.name,
        action='user_joined'
    ).filter(ActivityLog.timestamp >= sg_now() - timedelta(minutes=5)).first()
    
    # Only log if no recent join activity (avoid logging every page refresh)
    if not recent_activity:
        log_activity(user.conduct_id, user.name, 'user_joined', details=f"Conducting body accessed monitor")

    return render_template('monitor.html',
                         users=users_dict,
                         username=user.name,
                         user_id=user.id,
                         role=user.role,
                         conduct_id=user.conduct_id,
                         zones=WBGT_ZONES,
                         system_status=system_status,
                         history=get_recent_history(user.conduct_id))

# API Routes for real-time functionality

@app.route('/set_zone', methods=['POST'])
def set_zone():
    """Set WBGT zone for a user"""
    user_id = request.form.get('user_id')
    target_user_name = request.form.get('target_user')
    zone = request.form.get('zone')
    location = request.form.get('location')

    # If no target user specified, use current user
    if not target_user_name:
        current_user = User.query.get(user_id)
        if current_user:
            target_user_name = current_user.name

    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        conduct_id = user.conduct_id
        system_status = get_conduct_system_status(conduct_id)

        # Check system restrictions
        if system_status["cut_off"] and user.role != 'conducting_body':
            return jsonify({"error": "System is in cut-off mode"}), 403

        # Check mandatory rest period
        if system_status["cut_off_end_time"]:
            cut_off_end = datetime.strptime(system_status["cut_off_end_time"], "%H:%M:%S")
            cut_off_end_naive = sg_now().replace(hour=cut_off_end.hour, minute=cut_off_end.minute, second=cut_off_end.second)
            
            # Handle midnight rollover for mandatory rest period
            current_time = sg_now()
            if cut_off_end.hour < current_time.hour and (current_time.hour - cut_off_end.hour) > 12:
                cut_off_end_naive = cut_off_end_naive + timedelta(days=1)
                print(f"Server: Midnight rollover detected for mandatory rest end time: {system_status['cut_off_end_time']} moved to next day")
            
            if current_time < cut_off_end_naive and user.role != 'conducting_body':
                return jsonify({"error": "Mandatory rest period is still active"}), 403

        # Find target user
        target_user = User.query.filter_by(name=target_user_name, conduct_id=conduct_id).first()
        if not target_user:
            return jsonify({"error": "Target user not found"}), 404

        # Permission check
        if user.role == 'trainer' and target_user.name != user.name:
            return jsonify({"error": "Trainers can only set their own zone"}), 401

        # Prevent zone changes during rest and pending rest states
        if target_user.status == "resting" and user.role != 'conducting_body':
            return jsonify({"error": "Cannot start work cycle during rest period"}), 403

        if (target_user.work_completed and target_user.pending_rest) and user.role != 'conducting_body':
            return jsonify({"error": "Must start rest cycle before beginning new work cycle"}), 403

        # RENDER DEBUG: Log current user state before zone change
        print(f"RENDER DEBUG: Target user {target_user.name} current state - status: {target_user.status}, zone: {target_user.zone}, end_time: {target_user.end_time}, work_completed: {target_user.work_completed}")
        
        # RENDER FIX: Force cache invalidation to ensure fresh data for zone operations
        invalidate_user_cache(target_user.id)

        # Set zone and timing with WBGT zone overwrite logic
        now = sg_now()
        work_duration = WBGT_ZONES.get(zone, {}).get('work', 60)
        proposed_end = now + timedelta(minutes=work_duration, seconds=0, microseconds=0)
        print(f"RENDER DEBUG: New zone {zone} requested, work duration: {work_duration} minutes, proposed end: {proposed_end.strftime('%H:%M:%S')}")

        # WBGT zone overwrite logic - if user is currently working, use stricter (earlier) end time
        if target_user.status == 'working' and target_user.end_time and not target_user.work_completed:
            print(f"RENDER DEBUG: Zone overwrite triggered for {target_user.name} - current zone: {target_user.zone}, new zone: {zone}")
            print(f"RENDER DEBUG: Current end time: {target_user.end_time}, proposed new end would be: {(now + timedelta(minutes=work_duration)).strftime('%H:%M:%S')}")
            
            current_end_str = target_user.end_time
            current_end_naive = datetime.strptime(current_end_str, "%H:%M:%S")
            current_end = now.replace(hour=current_end_naive.hour, minute=current_end_naive.minute, second=current_end_naive.second)
            
            # Handle midnight rollover: if end time is earlier than start time,
            # it means the end time is on the next day
            if target_user.start_time:
                start_time_parts = target_user.start_time.split(':')
                start_hour = int(start_time_parts[0])
                end_hour = current_end_naive.hour
                
                # If end time is significantly earlier than start time, assume next day
                if end_hour < start_hour and (start_hour - end_hour) > 12:
                    current_end = current_end + timedelta(days=1)
                    print(f"RENDER DEBUG: Midnight rollover detected in zone overwrite for {target_user.name}: {current_end_str} moved to next day")
            
            # Only use the earlier time if the current end time is in the future
            if current_end > now:
                proposed_end = min(current_end, proposed_end)
                print(f"RENDER DEBUG: Zone overwrite applied - using earlier time: {proposed_end.strftime('%H:%M:%S')}")
            else:
                print(f"RENDER DEBUG: Zone overwrite NOT applied - current end time is in the past: {current_end} vs now: {now}")
        else:
            print(f"RENDER DEBUG: Zone overwrite NOT triggered for {target_user.name} - status: {target_user.status}, end_time: {target_user.end_time}, work_completed: {target_user.work_completed}")

        # Strip microseconds for consistent timing across platforms
        now = now.replace(microsecond=0)
        proposed_end = proposed_end.replace(microsecond=0)

        # Update user status and track most stringent zone
        target_user.status = 'working'
        target_user.zone = zone
        target_user.start_time = now.strftime('%H:%M:%S')
        target_user.end_time = proposed_end.strftime('%H:%M:%S')
        target_user.work_completed = False
        target_user.pending_rest = False
        
        # Track most stringent zone during work cycle
        target_user.most_stringent_zone = get_most_stringent_zone(zone, target_user.most_stringent_zone)
        print(f"STRINGENCY DEBUG: User {target_user.name} - Current zone: {zone}, Most stringent: {target_user.most_stringent_zone}")
        
        if hasattr(target_user, 'location'):
            target_user.location = location

        db.session.commit()

        # Invalidate cache
        invalidate_user_cache(target_user.id)

        # Log activity
        log_activity(conduct_id, target_user.name, 'start_work', zone)

        # Emit updates to conduct room - CRITICAL: This ensures real-time updates
        emit_user_update(conduct_id, target_user)

        return jsonify({
            "success": True,
            "user": target_user.name,
            "zone": zone,
            "start_time": target_user.start_time,
            "end_time": target_user.end_time
        })

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error setting zone: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/toggle_cut_off', methods=['POST'])
def toggle_cut_off():
    """Toggle cut-off mode for a conduct"""
    user_id = request.form.get('user_id')

    try:
        user = User.query.get(user_id)
        if not user or user.role != 'conducting_body':
            return jsonify({"error": "Unauthorized"}), 401

        conduct_id = user.conduct_id
        system_status = get_conduct_system_status(conduct_id)

        now = sg_now()

        if system_status["cut_off"]:
            # Deactivate cut-off, start mandatory rest
            system_status["cut_off"] = False
            system_status["cut_off_end_time"] = (now + timedelta(minutes=30)).strftime("%H:%M:%S")

            # Set all trainers to resting
            trainers = User.query.filter_by(conduct_id=conduct_id, role='trainer').all()
            for trainer in trainers:
                trainer.status = 'resting'
                trainer.zone = None
                trainer.start_time = now.strftime("%H:%M:%S")
                trainer.end_time = (now + timedelta(minutes=30)).strftime("%H:%M:%S")
                # IMPORTANT: Emit individual user updates for each trainer
                emit_user_update(conduct_id, trainer)
        else:
            # Activate cut-off
            system_status["cut_off"] = True
            system_status["cut_off_end_time"] = None

            # Set all trainers to idle
            trainers = User.query.filter_by(conduct_id=conduct_id, role='trainer').all()
            for trainer in trainers:
                trainer.status = 'idle'
                trainer.zone = None
                trainer.start_time = None
                trainer.end_time = None
                # IMPORTANT: Emit individual user updates for each trainer
                emit_user_update(conduct_id, trainer)

        db.session.commit()

        # CRITICAL: Emit system status update to ALL clients in conduct room
        emit_system_status_update(conduct_id, system_status)

        return jsonify({"success": True, "system_status": system_status})

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error toggling cut-off: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/stop_cycle', methods=['POST'])
def stop_cycle():
    """Stop current cycle early"""
    user_id = request.form.get('user_id')

    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        # Stop current cycle
        user.status = 'idle'
        user.zone = None
        user.start_time = None
        user.end_time = None
        user.work_completed = False
        user.pending_rest = False

        db.session.commit()

        # Invalidate cache
        invalidate_user_cache(user.id)

        # Log activity
        log_activity(user.conduct_id, user.name, 'early_completion')

        # CRITICAL: Emit user update to all clients in conduct room
        emit_user_update(user.conduct_id, user)

        return jsonify({"success": True})

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error stopping cycle: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/start_rest', methods=['POST'])
def start_rest():
    """Start rest cycle"""
    data = request.get_json() if request.is_json else request.form
    username = data.get('username')

    try:
        user = User.query.filter_by(name=username).first()
        if not user:
            return jsonify({"error": "User not found"}), 404

        # Start rest cycle with precise timing using most stringent zone
        now = sg_now()
        
        # Use most stringent zone for rest duration calculation
        zone_for_rest = user.most_stringent_zone or user.zone
        rest_duration = get_rest_duration_for_most_stringent_zone(zone_for_rest)
        print(f"STRINGENCY DEBUG: User {user.name} - Current zone: {user.zone}, Most stringent: {user.most_stringent_zone}, Rest duration: {rest_duration} min")

        # Handle test cycle differently (seconds vs minutes)
        if user.zone == 'test':
            end_time = now + timedelta(seconds=rest_duration)
        else:
            end_time = now + timedelta(minutes=rest_duration)

        # Store times with exact precision to ensure accurate duration tracking
        start_time_str = now.strftime('%H:%M:%S')
        end_time_str = end_time.strftime('%H:%M:%S')

        user.status = 'resting'
        user.start_time = start_time_str
        user.end_time = end_time_str
        
        # Reset most stringent zone tracker after starting rest
        user.most_stringent_zone = None
        
        print(f"TIMING DEBUG: Rest start - Now: {now}, End: {end_time}, Duration: {rest_duration}min")
        user.work_completed = False
        user.pending_rest = False

        db.session.commit()

        # Invalidate cache
        invalidate_user_cache(user.id)

        # Log activity with enhanced details showing stringent zone logic
        if zone_for_rest == 'test':
            # Convert minutes to seconds for test zone display (0.1667 min = 10 sec)
            rest_seconds = int(rest_duration * 60)
            log_activity(user.conduct_id, user.name, 'start_rest', user.zone, 
                        f"Started {rest_seconds} second rest period (based on most stringent zone: {zone_for_rest})")
        else:
            # Format minutes properly (remove unnecessary decimals)
            rest_minutes = int(rest_duration) if rest_duration == int(rest_duration) else rest_duration
            log_activity(user.conduct_id, user.name, 'start_rest', user.zone, 
                        f"Started {rest_minutes} minute rest period (based on most stringent zone: {zone_for_rest})")

        # CRITICAL: Emit user update to all clients in conduct room
        emit_user_update(user.conduct_id, user)

        return jsonify({
            "success": True,
            "start_time": user.start_time,
            "end_time": user.end_time
        })

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error starting rest: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/get_user_state/<username>')
def get_user_state(username):
    """Get current state of a user"""
    try:
        user = User.query.filter_by(name=username).first()
        if not user:
            return jsonify({"error": "User not found"}), 404

        return jsonify({
            "status": user.status,
            "zone": user.zone,
            "most_stringent_zone": user.most_stringent_zone,
            "start_time": user.start_time,
            "end_time": user.end_time,
            "work_completed": user.work_completed,
            "pending_rest": user.pending_rest
        })

    except Exception as e:
        logging.error(f"Error getting user state: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/clear_commands', methods=['POST'])
def clear_commands():
    """Clear all commands and reset all trainer interfaces in the conduct"""
    user_id = request.form.get('user_id')

    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        if user.role != 'conducting_body':
            return jsonify({"error": "Only conducting body can use this function"}), 401

        conduct_id = user.conduct_id

        # Reset all trainers in this conduct to initial idle state
        trainers = User.query.filter_by(conduct_id=conduct_id, role='trainer').all()

        for trainer in trainers:
            trainer.status = 'idle'
            trainer.zone = None
            trainer.start_time = None
            trainer.end_time = None
            trainer.work_completed = False
            trainer.pending_rest = False
            trainer.most_stringent_zone = None  # Reset stringent zone tracker

            # Invalidate cache
            invalidate_user_cache(trainer.id)

            # Log activity
            log_activity(conduct_id, trainer.name, 'interface_reset', details="Trainer interface reset by conducting body")

            # CRITICAL: Emit user update to all clients in conduct room
            emit_user_update(conduct_id, trainer)

        # Reset system status
        system_status = get_conduct_system_status(conduct_id)
        system_status["cut_off"] = False
        system_status["cut_off_end_time"] = None

        db.session.commit()

        # Emit system status update
        emit_system_status_update(conduct_id, system_status)

        # Log conducting body activity
        log_activity(conduct_id, user.name, 'clear_commands', details="All commands cleared and trainer interfaces reset")

        return jsonify({"success": True, "message": "All commands cleared and trainer interfaces reset successfully"})

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error clearing commands: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/get_server_time')
def get_server_time():
    """Get current server time for synchronization"""
    # Return UTC timestamp for proper client synchronization
    return jsonify({"timestamp": datetime.utcnow().timestamp()})

@app.route('/get_system_status')
def get_system_status():
    """Get current system status"""
    # For now return empty status, but in full implementation this would get the actual status
    return jsonify({"cut_off": False, "cut_off_end_time": None})

@app.route('/api/debug_rest_completion/<int:conduct_id>')
def debug_rest_completion(conduct_id):
    """Debug endpoint to check rest completion status"""
    try:
        # Get all resting users in this conduct
        resting_users = User.query.filter_by(conduct_id=conduct_id, status='resting').all()
        
        now = sg_now()
        current_time_str = now.strftime('%H:%M:%S')
        
        debug_info = {
            'current_time': current_time_str,
            'resting_users': [],
            'activity_logs': []
        }
        
        for user in resting_users:
            user_info = {
                'name': user.name,
                'zone': user.zone,
                'start_time': user.start_time,
                'end_time': user.end_time,
                'should_complete': False
            }
            
            if user.end_time:
                # Parse end time
                end_time_parts = user.end_time.split(':')
                end_time = now.replace(
                    hour=int(end_time_parts[0]),
                    minute=int(end_time_parts[1]),
                    second=int(end_time_parts[2]) if len(end_time_parts) > 2 else 0,
                    microsecond=0
                )
                
                user_info['should_complete'] = now >= end_time
                user_info['time_until_completion'] = str(end_time - now) if now < end_time else 'OVERDUE'
            
            debug_info['resting_users'].append(user_info)
        
        # Get recent activity logs
        recent_logs = ActivityLog.query.filter_by(conduct_id=conduct_id)\
                                     .order_by(ActivityLog.timestamp.desc())\
                                     .limit(10).all()
        
        debug_info['activity_logs'] = [{
            'timestamp': log.timestamp.isoformat(),
            'username': log.username,
            'action': log.action,
            'zone': log.zone,
            'details': log.details
        } for log in recent_logs]
        
        return jsonify(debug_info)
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/check_activity_history/<int:conduct_id>')
def check_activity_history(conduct_id):
    """Check recent activity history for debugging"""
    try:
        recent_logs = ActivityLog.query.filter_by(conduct_id=conduct_id)\
                                     .order_by(ActivityLog.timestamp.desc())\
                                     .all()
        
        return jsonify({
            'conduct_id': conduct_id,
            'total_logs': len(recent_logs),
            'recent_activity': [{
                'id': log.id,
                'timestamp': log.timestamp.isoformat(),
                'username': log.username,
                'action': log.action,
                'zone': log.zone,
                'details': log.details
            } for log in recent_logs]
        })
        
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/force_rest_completion/<username>')
def force_rest_completion(username):
    """Force rest completion for testing on Render deployment"""
    try:
        user = User.query.filter_by(name=username).first()
        if not user:
            return jsonify({"error": "User not found"}), 404
            
        if user.status != 'resting':
            return jsonify({"error": f"User {username} is not in resting state. Current status: {user.status}"}), 400
            
        now = sg_now()
        current_time_str = now.strftime('%H:%M:%S')
        
        print(f"FORCE REST COMPLETION: Triggering for {username}")
        
        # Store zone and conduct info before clearing for logging
        completed_zone = user.zone
        conduct_id = user.conduct_id
        user_name = user.name
        
        # Log completion BEFORE resetting user data - Enhanced for Render debugging
        try:
            # RENDER PRODUCTION FIX: Create activity log directly for better control
            activity_log = ActivityLog(
                conduct_id=conduct_id,
                username=user_name,
                action='completed_rest',
                zone=completed_zone,
                details=f"Rest cycle completed manually at {current_time_str}",
                timestamp=sg_now()
            )
            
            db.session.add(activity_log)
            db.session.flush()  # Ensure log is created
            print(f"FORCE RENDER DEBUG: Activity log created for {user_name}: completed_rest in {completed_zone}")
            
            # Reset user data
            user.status = 'idle'
            user.zone = None
            user.start_time = None
            user.end_time = None
            user.work_completed = False
            user.pending_rest = False
            
            # Commit BOTH activity log and user changes together
            db.session.commit()
            print(f"FORCE RENDER DEBUG: Combined database commit successful for {user_name}")
            
        except Exception as combined_error:
            print(f"FORCE RENDER DEBUG: Error in combined transaction: {combined_error}")
            db.session.rollback()
            return jsonify({"error": f"Database transaction failed: {combined_error}"}), 500
        
        invalidate_user_cache(user.id)
        
        # Emit user update
        emit_user_update(conduct_id, user)
        
        # Emit specific event for zone button re-enabling
        socketio.emit('rest_cycle_completed', {
            'user': user_name,
            'zone': completed_zone,
            'action': 'rest_cycle_completed'
        }, room=f'conduct_{conduct_id}')
        
        # Force immediate history refresh for monitors
        socketio.emit('history_update', {
            'history': get_recent_history(conduct_id)
        }, room=f'conduct_{conduct_id}')
        
        print(f"FORCE: Rest completion processed successfully for {user_name} in zone {completed_zone}")
        
        return jsonify({
            "success": True,
            "message": f"Rest completion forced for {username}",
            "previous_zone": completed_zone,
            "new_status": user.status
        })
        
    except Exception as e:
        print(f"FORCE: Error in manual rest completion: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/get_conduct_history/<int:conduct_id>')
def get_conduct_history(conduct_id):
    """Get activity history for a specific conduct"""
    try:
        history = get_recent_history(conduct_id)
        return jsonify({
            'success': True,
            'conduct_id': conduct_id,
            'history': history
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test_activity_log/<conduct_id>')
def test_activity_log(conduct_id):
    """Test endpoint to verify activity logging works on Render"""
    try:
        conduct_id = int(conduct_id)
        current_time_str = sg_now().strftime('%H:%M:%S')
        
        # Test creating an activity log
        test_log = ActivityLog(
            conduct_id=conduct_id,
            username="RENDER_TEST",
            action='test_log',
            zone='test',
            details=f"Test activity log created at {current_time_str}",
            timestamp=sg_now()
        )
        
        db.session.add(test_log)
        db.session.commit()
        
        # Retrieve recent logs to verify
        recent_logs = get_recent_history(conduct_id, 5)  # Keep limit for test endpoint
        
        return jsonify({
            "success": True,
            "message": "Activity log test successful",
            "test_time": current_time_str,
            "recent_logs": recent_logs
        })
        
    except Exception as e:
        print(f"TEST LOG ERROR: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/test_rest_completion/<username>')
def test_rest_completion(username):
    """Test endpoint to manually trigger rest completion for debugging"""
    try:
        user = User.query.filter_by(name=username).first()
        if not user:
            return jsonify({"error": "User not found"}), 404
            
        if user.status != 'resting':
            return jsonify({"error": f"User {username} is not in resting state. Current status: {user.status}"}), 400
            
        now = sg_now()
        current_time_str = now.strftime('%H:%M:%S')
        
        print(f"MANUAL TEST: Triggering rest completion for {username}")
        print(f"User status before: {user.status}, zone: {user.zone}, end_time: {user.end_time}")
        
        # Store zone and conduct info before clearing for logging
        completed_zone = user.zone
        conduct_id = user.conduct_id
        user_name = user.name
        
        # Log completion BEFORE resetting user data
        try:
            log_activity(conduct_id, user_name, 'completed_rest', completed_zone, 
                        f"Rest cycle completed manually at {current_time_str}")
            print(f"MANUAL TEST: Activity logged for {user_name}: completed_rest in {completed_zone}")
        except Exception as log_error:
            print(f"MANUAL TEST: Error logging rest completion: {log_error}")
        
        # Reset user data
        user.status = 'idle'
        user.zone = None
        user.start_time = None
        user.end_time = None
        user.work_completed = False
        user.pending_rest = False
        
        # Commit database changes
        try:
            db.session.commit()
            print(f"MANUAL TEST: Database committed for {user_name} rest completion")
        except Exception as commit_error:
            print(f"MANUAL TEST: Database commit error: {commit_error}")
            db.session.rollback()
            return jsonify({"error": f"Database commit failed: {commit_error}"}), 500
        
        invalidate_user_cache(user.id)
        
        # Emit user update
        emit_user_update(conduct_id, user)
        
        # Emit specific event for zone button re-enabling
        socketio.emit('rest_cycle_completed', {
            'user': user_name,
            'zone': completed_zone,
            'action': 'rest_cycle_completed'
        }, room=f'conduct_{conduct_id}')
        
        # Force immediate history refresh for monitors
        socketio.emit('history_update', {
            'history': get_recent_history(conduct_id)
        }, room=f'conduct_{conduct_id}')
        
        print(f"MANUAL TEST: Rest completion processed successfully for {user_name} in zone {completed_zone}")
        
        return jsonify({
            "success": True,
            "message": f"Rest completion triggered for {username}",
            "previous_zone": completed_zone,
            "new_status": user.status
        })
        
    except Exception as e:
        print(f"MANUAL TEST: Error in manual rest completion: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/remove_user', methods=['POST'])
def remove_user():
    """Remove a user from the conduct"""
    user_id = request.form.get('user_id')
    target_user_name = request.form.get('target_user')

    try:
        user = User.query.get(user_id)
        if not user:
            return jsonify({"error": "User not found"}), 404

        if user.role != 'conducting_body':
            return jsonify({"error": "Only conducting body can remove users"}), 401

        conduct_id = user.conduct_id

        # Find target user
        target_user = User.query.filter_by(name=target_user_name, conduct_id=conduct_id).first()
        if not target_user:
            return jsonify({"error": "Target user not found"}), 404

        if target_user.role == 'conducting_body':
            return jsonify({"error": "Cannot remove conducting body users"}), 403

        # Log removal activity
        log_activity(conduct_id, user.name, 'user_removed', details=f"Removed user {target_user_name} from conduct")

        # Remove the user from the database
        db.session.delete(target_user)
        db.session.commit()

        # Invalidate cache
        invalidate_user_cache(target_user.id)

        # Emit user removal update to all clients in conduct room
        socketio.emit('user_removed', {
            'user': target_user_name
        }, room=f'conduct_{conduct_id}')

        return jsonify({"success": True, "message": f"User {target_user_name} removed successfully"})

    except Exception as e:
        db.session.rollback()
        logging.error(f"Error removing user: {e}")
        return jsonify({"error": "Internal server error"}), 500

@app.route('/force_work_completion_check/<username>')
def force_work_completion_check(username):
    """Manually trigger work completion check for a user"""
    try:
        user = User.query.filter_by(name=username).first()
        if not user:
            return jsonify({"error": "User not found"}), 404

        if user.work_completed and user.pending_rest and user.zone:
            # Show work complete modal
            show_work_complete_modal(user.name, user.zone)
            
            # Emit work cycle completed event
            socketio.emit('work_cycle_completed', {
                'username': user.name,
                'zone': user.zone,
                'rest_time': WBGT_ZONES.get(user.zone, {}).get('rest', 15),
                'action': 'work_cycle_completed'
            }, room=f'conduct_{user.conduct_id}')
            
            return jsonify({"success": True, "message": "Work completion notification sent"})
        else:
            return jsonify({"success": False, "message": "No pending work completion found"})

    except Exception as e:
        logging.error(f"Error in force work completion check: {e}")
        return jsonify({"error": "Internal server error"}), 500

# Socket.IO events

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    print(f'Client connected: {request.sid}')
    # Start background task when first client connects
    global background_task_started
    if not background_task_started:
        start_background_tasks()
        background_task_started = True

@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    print(f'Client disconnected: {request.sid}')

@socketio.on('join_conduct')
def handle_join_conduct(data):
    """Join a conduct room for real-time updates"""
    conduct_id = data.get('conduct_id')
    if conduct_id:
        join_room(f'conduct_{conduct_id}')
        print(f'Client {request.sid} joined conduct room: {conduct_id}')

        # Send current system status immediately when joining
        system_status = get_conduct_system_status(conduct_id)
        emit('system_status_update', system_status)

@socketio.on('leave_conduct')
def handle_leave_conduct(data):
    """Leave a conduct room"""
    conduct_id = data.get('conduct_id')
    if conduct_id:
        leave_room(f'conduct_{conduct_id}')
        print(f'Client {request.sid} left conduct room: {conduct_id}')

@app.route('/change_password', methods=['GET', 'POST'])
def change_password():
    """Change password for battalion or company"""
    if request.method == 'POST':
        password_type = request.form.get('password_type')
        
        if password_type == 'battalion':
            battalion_name = request.form.get('battalion_name', '').strip()
            current_password = request.form.get('current_password', '').strip()
            new_password = request.form.get('new_password', '').strip()
            confirm_password = request.form.get('confirm_password', '').strip()
            
            # Validation
            if not all([battalion_name, current_password, new_password, confirm_password]):
                flash('All fields are required.', 'error')
                return render_template('change_password.html')
            
            if new_password != confirm_password:
                flash('New passwords do not match.', 'error')
                return render_template('change_password.html')
            
            if len(new_password) < 6:
                flash('Password must be at least 6 characters long.', 'error')
                return render_template('change_password.html')
            
            # Find battalion (case sensitive)
            battalion = Battalion.query.filter_by(name=battalion_name).first()
            if not battalion:
                flash('Battalion not found.', 'error')
                return render_template('change_password.html')
            
            # Verify current password
            if not battalion.check_password(current_password):
                flash('Current password is incorrect.', 'error')
                return render_template('change_password.html')
            
            # Update password
            battalion.set_password(new_password)
            db.session.commit()
            
            flash(f'Battalion password for {battalion.name.title()} updated successfully!', 'success')
            return render_template('change_password.html')
            
        elif password_type == 'company':
            battalion_name = request.form.get('company_battalion_name', '').strip()
            company_name = request.form.get('company_name', '').strip()
            current_password = request.form.get('company_current_password', '').strip()
            new_password = request.form.get('company_new_password', '').strip()
            confirm_password = request.form.get('company_confirm_password', '').strip()
            
            # Validation
            if not all([battalion_name, company_name, current_password, new_password, confirm_password]):
                flash('All fields are required.', 'error')
                return render_template('change_password.html')
            
            if new_password != confirm_password:
                flash('New passwords do not match.', 'error')
                return render_template('change_password.html')
            
            if len(new_password) < 6:
                flash('Password must be at least 6 characters long.', 'error')
                return render_template('change_password.html')
            
            # Find battalion first (case sensitive)
            battalion = Battalion.query.filter_by(name=battalion_name).first()
            if not battalion:
                flash('Battalion not found.', 'error')
                return render_template('change_password.html')
            
            # Find company within the battalion (case sensitive)
            company = Company.query.filter_by(
                name=company_name, 
                battalion_id=battalion.id
            ).first()
            if not company:
                flash(f'Company {company_name} not found in {battalion_name} Battalion.', 'error')
                return render_template('change_password.html')
            
            # Verify current password
            if not company.check_password(current_password):
                flash('Current password is incorrect.', 'error')
                return render_template('change_password.html')
            
            # Update password
            company.set_password(new_password)
            db.session.commit()
            
            flash(f'Company password for {company.name.title()} Company updated successfully!', 'success')
            return render_template('change_password.html')
    
    return render_template('change_password.html')

# Schedule work completion checks every 5 seconds
def start_background_tasks():
    """Start background tasks"""
    def run_checks():
        conduct_check_counter = 0
        while True:
            eventlet.sleep(1)  # Check every 1 second for responsive completion detection
            check_user_cycles()
            
            # Check conduct activity every 60 seconds (1 minute)
            conduct_check_counter += 1
            if conduct_check_counter >= 60:
                check_conduct_activity()
                conduct_check_counter = 0

    eventlet.spawn(run_checks)
    print("Background task started for work cycle monitoring (1-second intervals) and conduct activity checking (1-minute intervals)")

# Add cleanup handler
@app.teardown_appcontext
def cleanup_db(error):
    """Clean up database connections"""
    if error:
        db.session.rollback()
    db.session.remove()

# Expose app for WSGI deployment (like gunicorn)
if __name__ == '__main__':
    # Start background tasks
    start_background_tasks()
    background_task_started = True

    # Use SocketIO's built-in server instead of Gunicorn for better WebSocket support
    socketio.run(app, host='0.0.0.0', port=5000, debug=False, use_reloader=False, 
                 allow_unsafe_werkzeug=True, log_output=False)
else:
    # For deployment with gunicorn, start background tasks
    start_background_tasks()
    background_task_started = True
