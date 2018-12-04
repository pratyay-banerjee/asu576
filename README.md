# Extending and Improving AllenNLP BiDAF for Squad 1 to Squad 2 with No Answer

## 0 Framework Inspiration
For more details check out [this article](https://medium.com/swlh/deep-learning-for-text-made-easy-with-allennlp-62bc79d41f31) 

## 1 SQuAD 2
This contains passages and questions which might have answers present in the provided passage. The model should be aware of both the capability to decide if an answer is present or if present what is the answer.

## 2 Data inputs
The file download.sh contains the commands to fetch the dataset. We modify the provided DataSetReader of AllenNlp to be able to read the SQuAD2 dataset.

## 3 The model
We implement several models, starting from the baseline model of the AllenNLP's BiDAF modified for No-Answer. The other models are iterative improvements like changing Similarity Function of the Attention Layer, Adding different types of Attention.

## 4 Training the model
The trainer uses the Adam optimizer for varied number of epochs. The configs are present in the experiments directory, with file names reflecting a very description of the experiments. The file commands.txt provides how to run the training. Patience and other configurables are present in the json experiment files.

## 5 Predictions
For Predictions, we use the following command :

`CUDA_VISIBLE_DEVICES=1 allennlp predict  --use-dataset-reader  --cuda-device 0 --include-package squad2.dataset_readers  --include-package squad2.models  --predictor machine-comprehension trained/bidaf-sim1/model.tar.gz squad2/data/dev-v2.0.json --output-file trained/bidaf-sim1-dev-output.json --silent`


