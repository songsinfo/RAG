package dev.rag.evaluation;

import dev.rag.common.ApiException;
import org.junit.jupiter.api.Test;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class EvaluationRunnerTest {

 @Test
 void retrievalOnlyPermitsBm25WithoutModelQueryTransformsOrReranking() {
  Map<String, Object> validBm25 = Map.of(
   "retrieval", Map.of("mode", "BM25"),
   "query", Map.of("rewrite", false, "hyde", false, "multiQuery", false),
   "post", Map.of("rerank", false, "mmr", false)
  );
  assertDoesNotThrow(() -> EvaluationRunner.checkNoModel(validBm25));

  Map<String, Object> denseConfig = Map.of(
   "retrieval", Map.of("mode", "DENSE")
  );
  var e1 = assertThrows(ApiException.class, () -> EvaluationRunner.checkNoModel(denseConfig));
  assertEquals("MODEL_REQUIRED", e1.code);

  Map<String, Object> rewriteConfig = Map.of(
   "retrieval", Map.of("mode", "BM25"),
   "query", Map.of("rewrite", true)
  );
  var e2 = assertThrows(ApiException.class, () -> EvaluationRunner.checkNoModel(rewriteConfig));
  assertEquals("MODEL_REQUIRED", e2.code);

  Map<String, Object> rerankConfig = Map.of(
   "retrieval", Map.of("mode", "BM25"),
   "post", Map.of("rerank", true)
  );
  var e3 = assertThrows(ApiException.class, () -> EvaluationRunner.checkNoModel(rerankConfig));
  assertEquals("MODEL_REQUIRED", e3.code);
 }

 @Test
 void invalidNumbersAreRejected() {
  assertEquals(3.14, EvaluationRunner.number("3.14"), 1e-6);
  assertEquals(10.0, EvaluationRunner.number(10), 1e-6);
  assertThrows(ApiException.class, () -> EvaluationRunner.number("abc"));
  assertThrows(ApiException.class, () -> EvaluationRunner.number("NaN"));
  assertThrows(ApiException.class, () -> EvaluationRunner.number("Infinity"));
 }

 @Test
 void deterministicHashMatchesIdenticalContent() {
  Map<String, Object> m1 = new LinkedHashMap<>();
  m1.put("caseKey", "K1");
  m1.put("split", "DEV");

  Map<String, Object> m2 = new LinkedHashMap<>();
  m2.put("caseKey", "K1");
  m2.put("split", "DEV");

  assertArrayEquals(EvaluationCatalog.hash(m1), EvaluationCatalog.hash(m2));
 }

 @Test
 void objectAndMapParsersValidateStructure() {
  assertNotNull(EvaluationCatalog.obj(null));
  assertTrue(EvaluationCatalog.maps(null).isEmpty());
  assertThrows(ApiException.class, () -> EvaluationCatalog.maps("not-a-list"));
 }
}
