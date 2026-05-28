sudo apt update -y
sudo apt install -y emacs 
sudo apt install -y elixir 
sudo apt install -y dc xxd cal


cp dotemacs ~/.emacs
cp sprite-dotmybashrc ~/.mybashrc
echo source .mybashrc >> ~/.profile

git config user.name "CK Tan" && git config user.email "cktanx@gmail.com"
git config pull.rebase true

cp sprite-idle-killer.py ~/.local/bin/sprite-idle-killer.py
python3 ~/.local/bin/sprite-idle-killer.py
