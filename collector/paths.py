from pathlib import Path
HOME=Path(".cache/feeds")
HOME.mkdir(parents=True, exist_ok=True)
DB=str(HOME/"feeds.sqlite")
