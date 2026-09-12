package dev.rag.search;
import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
class RankingTest {
 @Test void representationsDoNotMultiplyCanonicalVotes(){
  var a=List.of(new Ranking.Hit("a",3),new Ranking.Hit("a",2),new Ranking.Hit("b",1));
  var fused=Ranking.rrf(List.of(a),List.of(1d),60);
  assertEquals(List.of("a","b"),fused.stream().map(Ranking.Hit::id).toList());
  assertEquals(1d/61,fused.getFirst().score(),1e-12);
  assertEquals(1d/62,fused.get(1).score(),1e-12);
 }
 @Test void tiedFusionIsDeterministic(){assertEquals("a",Ranking.rrf(List.of(List.of(new Ranking.Hit("b",1)),List.of(new Ranking.Hit("a",1))),List.of(.5,.5),60).getFirst().id());}
 @Test void mmrCanSelectComplementaryEvidence(){
  var hits=List.of(new Ranking.Hit("a",1),new Ranking.Hit("b",.99),new Ranking.Hit("c",.9));
  var vectors=Map.of("a",List.of(1d,0d),"b",List.of(1d,0d),"c",List.of(0d,1d));
  assertEquals(List.of("a","c"),Ranking.mmr(hits,vectors,2,.3).stream().map(Ranking.Hit::id).toList());
 }
 @Test void incompatibleVectorsFail(){assertThrows(IllegalArgumentException.class,()->Ranking.cosine(List.of(1d),List.of(1d,2d)));}
}
