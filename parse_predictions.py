import sys
import json


print("Parse Predicted File.")
print("usage: python parse_predictions.py src_file dest_file")

if len(sys.argv) < 3:
    print("See usage!!")
    sys.exit(1)

src_file = sys.argv[1]
dest_file = sys.argv[2]

fi = open(src_file,"r")
fo = open(dest_file,"w+")

lines = fi.readlines()


passage_dict = {}
all_keys=['passage_question_attention', 'span_start_logits', 'span_start_probs', 'span_end_logits', 'span_end_probs', 'best_span', 'loss', 'na_logits', 'na_probs', 'best_span_str', 'question_tokens', 'passage_tokens']
w_keys=['best_span','na_probs','best_span_str']

for line in lines:
    l = line.strip()
    r = json.loads(l)
    if str(r['passage_tokens']) not in passage_dict.keys():
        passage_dict[str(r['passage_tokens'])] = []
    rq = [r['question_tokens'],r['best_span'],r['na_probs'],r['best_span_str']]
    passage_dict[str(r['passage_tokens'])].append(rq)

fi.close()

fo.write("Passage Followed by All Questions in Passage.\n")
i=0
for k in passage_dict.keys():
    fo.write("Passage :"+str(i)+"\n")
    for token in k.split():
        fo.write(token+" ")
    fo.write("\n")
    fo.write("Questions:\n")
    rq = passage_dict[k]

    for q in rq:
        for token in q[0]:
            fo.write(token + " ")
        fo.write("\n")
        fo.write("Best Span:"+str(q[1])+"\n")
        fo.write("Na_Probs:"+str(q[2])+"\n")
        fo.write("Answer:"+str(q[3])+"\n")
    fo.write("\n")
    i+=1
        
fo.close()
