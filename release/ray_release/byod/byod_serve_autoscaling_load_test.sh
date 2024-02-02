#!/bin/bash
# This script is used to build an extra layer on top of the base anyscale/ray image 

export CINDY=zhang

pip install locust==2.16.1

sudo apt-get update
sudo apt-get install curl -y
curl -o /home/ray/default/locustfile.py https://gist.githubusercontent.com/zcin/daffaf8b25e961728070dab5c43feeb6/raw/741dd04ea003be5a51acd425797e88b181289c76/locustfile.py