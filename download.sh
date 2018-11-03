mkdir squad2/data
mkdir embedding

# Download SQuAD
wget https://rajpurkar.github.io/SQuAD-explorer/dataset/train-v2.0.json -O squad2/data/train-v2.0.json
wget https://rajpurkar.github.io/SQuAD-explorer/dataset/dev-v2.0.json -O squad2/data/dev-v2.0.json

# Download glove
 wget https://worksheets.codalab.org/rest/bundles/0x15a09c8f74f94a20bec0b68a2e6703b3/contents/blob/ -O embedding/glove.6B.100d.txt