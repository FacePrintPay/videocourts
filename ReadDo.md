# Read & Do — VideoCourts™ Minimal Monorepo

Current folder: /storage/emulated/0/VideoCourts

Files:
- index.html     → Live landing page (termux-open index.html to view)
- README.md      → Institutional overview
- ReadDo.md      → This guide

Test:
termux-open index.html

Git & Deploy:
git init
git add .
git commit -m "Live minimal landing from shared storage"
git remote add origin https://github.com/VideoCourts/VideoCourts.git   # fix repo name if needed
git push -u origin main   # Username: faceprintpay   Password: your PAT

Deploy:
npx vercel --prod

Success = live URL from Vercel.
