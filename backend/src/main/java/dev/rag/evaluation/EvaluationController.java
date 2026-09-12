package dev.rag.evaluation;

import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;

import java.util.*;
import static dev.rag.common.Db.params;

@RestController
@RequestMapping("/api/v1/admin/eval")
public class EvaluationController {
 private final AccessPolicy access;
 private final EvaluationCatalog catalog;
 private final EvaluationRunner runner;

 public EvaluationController(AccessPolicy access, EvaluationCatalog catalog, EvaluationRunner runner) {
  this.access = access;
  this.catalog = catalog;
  this.runner = runner;
 }

 @GetMapping("/datasets")
 public Object datasets(HttpServletRequest r) {
  return catalog.datasets(access.actor(r));
 }

 @PostMapping("/datasets")
 public Object createDataset(HttpServletRequest r, @RequestBody Map<String, Object> body) {
  return catalog.createDataset(access.actor(r), body);
 }

 @PostMapping("/datasets/{id}/versions")
 public Object createVersion(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
  var a = access.actor(r);
  String sourceId = Json.str(body, "sourceVersionId");
  if (!sourceId.isBlank()) {
   return catalog.copy(a, sourceId);
  }
  return catalog.copy(a, id);
 }

 @GetMapping("/dataset-versions/{versionId}/cases")
 public Object cases(HttpServletRequest r, @PathVariable String versionId) {
  return catalog.cases(access.actor(r), versionId);
 }

 @SuppressWarnings("unchecked")
 @PostMapping("/dataset-versions/{versionId}/cases")
 public Object saveCases(HttpServletRequest r, @PathVariable String versionId, @RequestBody Map<String, Object> body) {
  var a = access.actor(r);
  List<Map<String, Object>> list;
  if (body.get("cases") instanceof List<?> raw) {
   list = raw.stream().filter(Map.class::isInstance).map(m -> (Map<String, Object>) m).toList();
  } else {
   list = List.of(body);
  }
  return catalog.save(a, versionId, list);
 }

 @PostMapping("/dataset-versions/{versionId}/freeze")
 public Object freeze(HttpServletRequest r, @PathVariable String versionId) {
  return catalog.freeze(access.actor(r), versionId);
 }

 @PatchMapping("/cases/{id}")
 public Object updateCase(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
  return catalog.update(access.actor(r), id, body);
 }

 @DeleteMapping("/cases/{id}")
 public Object deleteCase(HttpServletRequest r, @PathVariable String id) {
  catalog.delete(access.actor(r), id);
  return params("status", "DELETED");
 }

 @GetMapping("/snapshots")
 public Object snapshots(HttpServletRequest r) {
  return catalog.snapshots(access.actor(r));
 }

 @PostMapping("/snapshots")
 public Object createSnapshot(HttpServletRequest r, @RequestBody Map<String, Object> body) {
  return catalog.snapshot(access.actor(r), body);
 }

 @PostMapping("/estimates")
 public Object estimate(HttpServletRequest r, @RequestBody Map<String, Object> body) {
  return runner.estimate(access.actor(r), body);
 }

 @PostMapping("/runs")
 public Object start(HttpServletRequest r, @RequestBody Map<String, Object> body, @RequestHeader(value = "Idempotency-Key", required = false) String key) {
  String effectiveKey = (key != null && !key.isBlank()) ? key : UUID.randomUUID().toString();
  return runner.start(access.actor(r), body, effectiveKey);
 }

 @GetMapping("/runs/{id}")
 public Object getRun(HttpServletRequest r, @PathVariable String id) {
  return runner.get(access.actor(r), id);
 }

 @GetMapping("/runs/{id}/results")
 public Object runResults(HttpServletRequest r, @PathVariable String id) {
  return runner.results(access.actor(r), id);
 }

 @PostMapping("/runs/{id}/cancel")
 public Object cancelRun(HttpServletRequest r, @PathVariable String id) {
  return runner.cancel(access.actor(r), id);
 }

 @PostMapping("/runs/{id}/exports")
 public Object exportRun(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
  return runner.export(access.actor(r), id, body);
 }

 @PostMapping("/results/{id}/reviews")
 public Object reviewResult(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
  return runner.review(access.actor(r), id, body);
 }

 @GetMapping("/comparisons")
 public Object compare(HttpServletRequest r, @RequestParam String runIds) {
  var a = access.actor(r);
  List<String> ids = Arrays.stream(runIds.split(",")).map(String::trim).filter(s -> !s.isEmpty()).toList();
  return runner.compare(a, ids);
 }
}
