python up.py --check   # writes nothing
python up.py
git rm up.py
BLOG_REPO=/workspaces/rnvizion.github.io python tests/test_publish_gate.py
git rm in.sh
git add -A && git commit -m "fix: env forwarding, devcontainer paths, gitignore; guard the publish gate"
