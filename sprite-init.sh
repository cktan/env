sudo apt update -y
sudo apt install -y emacs 
sudo apt install -y elixir 


cp dotemacs ~/.emacs
cp sprite-dotmybashrc ~/.mybashrc
echo source .mybashrc >> ~/.profile
