sudo apt update -y
sudo apt install -y emacs 
sudo apt install -y elixir 
sudo apt install -y dc xxd cal


cp dotemacs ~/.emacs
cp sprite-dotmybashrc ~/.mybashrc
echo source .mybashrc >> ~/.profile

git config user.name "CK Tan" && git config user.email "cktanx@gmail.com"
git config pull.rebase true

cp sprite_idle_killer.py ~/

#mkdir -p  ~/.claude/skills/serve-tmp
#cp skills/serve-tmp.md ~/.claude/skills/serve-tmp/

# install code graph
# npm i -g @colbymchenry/codegraph
