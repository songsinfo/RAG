package dev.rag.evaluation;
import java.util.*;
public final class EvaluationMetrics {
 private EvaluationMetrics(){}
 public static Double ratio(long numerator,long denominator){return denominator==0?null:(double)numerator/denominator;}
 public static Map<String,Double> retrieval(List<Set<String>> groups,List<Set<String>> ranked,int k){
  if(k<1)throw new IllegalArgumentException("k must be positive");
  Map<String,Double> result=new LinkedHashMap<>();
  if(groups.isEmpty()){result.put("hit",null);result.put("recall",null);result.put("mrr",null);return result;}
  if(groups.stream().anyMatch(Set::isEmpty))throw new IllegalArgumentException("Empty fact group");
  Set<Integer> found=new HashSet<>();int first=0;
  for(int rank=0;rank<Math.min(k,ranked.size());rank++){
   boolean relevant=false;
   for(int g=0;g<groups.size();g++)if(!Collections.disjoint(groups.get(g),ranked.get(rank))){found.add(g);relevant=true;}
   if(relevant&&first==0)first=rank+1;
  }
  result.put("hit",found.isEmpty()?0.0:1.0);result.put("recall",(double)found.size()/groups.size());result.put("mrr",first==0?0.0:1.0/first);return result;
 }
 public static Double ndcg(List<Integer> ranked,List<Integer> ideal,int k){
  if(k<1)throw new IllegalArgumentException("k must be positive");
  if(ranked.stream().anyMatch(g->g<0||g>3)||ideal.stream().anyMatch(g->g<0||g>3))throw new IllegalArgumentException("Grade must be 0..3; unjudged is not zero");
  double denominator=dcg(ideal.stream().sorted(Comparator.reverseOrder()).toList(),k);return denominator==0?null:dcg(ranked,k)/denominator;
 }
 private static double dcg(List<Integer> grades,int k){double total=0;for(int i=0;i<Math.min(k,grades.size());i++)total+=(Math.pow(2,grades.get(i))-1)/(Math.log(i+2)/Math.log(2));return total;}
 public static Long percentile(List<Long> values,double quantile){
  if(quantile<=0||quantile>1)throw new IllegalArgumentException("quantile must be (0,1]");
  if(values.isEmpty())return null;var sorted=values.stream().sorted().toList();return sorted.get((int)Math.ceil(sorted.size()*quantile)-1);
 }
}

