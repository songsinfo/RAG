package dev.rag.evaluation;
import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;
class EvaluationMetricsTest {
 @Test void oneOfThreeFactsIsAHitButOnlyThirdRecall(){
  var m=EvaluationMetrics.retrieval(List.of(Set.of("a"),Set.of("b"),Set.of("c")),List.of(Set.of("a")),5);
  assertEquals(1.0,m.get("hit"));assertEquals(1.0/3,m.get("recall"));assertEquals(1.0,m.get("mrr"));
 }
 @Test void duplicateChunksAndAlternativeEvidenceDoNotInventCoverage(){
  var m=EvaluationMetrics.retrieval(List.of(Set.of("a","a2"),Set.of("b")),List.of(Set.of("a2"),Set.of("a"),Set.of("a2")),5);
  assertEquals(0.5,m.get("recall"));
 }
 @Test void emptyGoldIsNotPerfectAndFailedAnswerableQuestionIsZero(){
  assertNull(EvaluationMetrics.retrieval(List.of(),List.of(),5).get("recall"));
  assertEquals(0.0,EvaluationMetrics.retrieval(List.of(Set.of("a")),List.of(),5).get("recall"));
 }
 @Test void zeroClaimsCannotScorePerfectFaithfulness(){assertNull(EvaluationMetrics.ratio(0,0));}
 @Test void dcgUsesGradedRelevanceAndRejectsUnknownGrades(){
  assertEquals(1.0,EvaluationMetrics.ndcg(List.of(3,2,0),List.of(3,2,0),3),1e-9);
  assertTrue(EvaluationMetrics.ndcg(List.of(0,2,3),List.of(3,2,0),3)<1);
  assertNull(EvaluationMetrics.ndcg(List.of(0),List.of(0),1));
  assertThrows(IllegalArgumentException.class,()->EvaluationMetrics.ndcg(List.of(-1),List.of(3),1));
 }
 @Test void retrievalResultIncludesNdcgForTheSameRankedCandidates(){
  var m=EvaluationMetrics.retrieval(List.of(Set.of("a"),Set.of("b")),List.of(Set.of("b"),Set.of("a")),List.of(1,3),List.of(3,1),2);
  assertEquals(1.0,m.get("recall"));assertTrue(m.get("ndcg")<1.0);assertTrue(m.get("ndcg")>0.0);
 }
 @Test void nearestRankPercentileUsesAllObservedDurations(){
  assertEquals(1000L,EvaluationMetrics.percentile(List.of(1L,2L,3L,1000L),.95));
 }
}
