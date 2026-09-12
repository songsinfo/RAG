package dev.rag.search;
import java.util.*;
public final class Ranking {
 private Ranking(){}
 public record Hit(String id,double score){}
 public static List<Hit> rrf(List<List<Hit>> routes,List<Double> weights,int k){
  if(routes.size()!=weights.size()||k<1)throw new IllegalArgumentException("Invalid RRF configuration");
  Map<String,Double> scores=new HashMap<>();
  for(int i=0;i<routes.size();i++){Set<String> seen=new HashSet<>();int rank=0;for(Hit h:routes.get(i))if(seen.add(h.id()))scores.merge(h.id(),weights.get(i)/(k+(double)++rank),Double::sum);}
  return scores.entrySet().stream().map(e->new Hit(e.getKey(),e.getValue())).sorted(Comparator.comparingDouble(Hit::score).reversed().thenComparing(Hit::id)).toList();
 }
 public static double cosine(List<Double>a,List<Double>b){
  if(a.size()!=b.size()||a.isEmpty())throw new IllegalArgumentException("Incompatible vectors");double dot=0,x=0,y=0;
  for(int i=0;i<a.size();i++){dot+=a.get(i)*b.get(i);x+=a.get(i)*a.get(i);y+=b.get(i)*b.get(i);}
  if(x==0||y==0)throw new IllegalArgumentException("Zero vector");return Math.max(-1,Math.min(1,dot/Math.sqrt(x*y)));
 }
 public static List<Hit> mmr(List<Hit> input,Map<String,List<Double>>vectors,int count,double lambda){
  if(lambda<0||lambda>1)throw new IllegalArgumentException("Invalid MMR lambda");if(input.isEmpty())return List.of();
  double min=input.stream().mapToDouble(Hit::score).min().orElseThrow(),max=input.stream().mapToDouble(Hit::score).max().orElseThrow();
  List<Hit>remaining=new ArrayList<>(input),selected=new ArrayList<>();
  while(!remaining.isEmpty()&&selected.size()<count){Hit best=null;double bestScore=-Double.MAX_VALUE;
   for(Hit h:remaining){double relevance=max==min?1:(h.score()-min)/(max-min),redundancy=0;
    for(Hit s:selected)redundancy=Math.max(redundancy,(cosine(Objects.requireNonNull(vectors.get(h.id())),Objects.requireNonNull(vectors.get(s.id())))+1)/2);
    double value=lambda*relevance-(1-lambda)*redundancy;if(value>bestScore){best=h;bestScore=value;}}
   selected.add(best);remaining.remove(best);
  }return selected;
 }
}
