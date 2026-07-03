from pathlib import Path
p = Path(r"D:\TASK\HeiShi\Project\HeyMarketApp\Documents\DOC-005_Client_Requirements_Master_Checklist.md")
p.write_text("# DOC-005\n\nSee chat for full content - regenerate from agent.\n", encoding="utf-8")
print("stub", p)
