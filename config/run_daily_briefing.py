import os, sys
os.chdir('S:\\Projects\\J.A.R.V.I.S')
sys.path.insert(0, 'S:\\Projects\\J.A.R.V.I.S')
from plugins.daily_tasks import run_scheduled_briefing
run_scheduled_briefing()
