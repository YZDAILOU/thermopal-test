# Setup for Thermopal for local development and testing
1. Set up Python locally

Make sure you have Python 3.10+ installed.
Check with:

python --version

2. Create a virtual environment

Inside your project folder:

python -m venv venv


Activate it:

Windows (PowerShell):

.\venv\Scripts\activate

3. Install dependencies

Replit usually has a poetry.lock or requirements.txt.
Check if requirements.txt exists. If yes, install:

pip install -r requirements.txt

4. Handle environment variables

<!-- On Replit, environment variables were likely set in the “Secrets” tab.
You’ll need to create a .env file in your project root (ignored by git). Example:

FLASK_ENV=development
SECRET_KEY=your-secret-key
DATABASE_URL=mysql://user:pass@localhost/dbname -->


Then install python-dotenv (if not already):

pip install python-dotenv

5. Run Flask with:

flask --app main run

