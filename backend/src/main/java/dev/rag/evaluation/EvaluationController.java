package dev.rag.evaluation;

import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;

import java.util.*;

import static dev.rag.common.Db.params;

/**
 * 离线自动化评测与基准对比 REST 控制器。
 * <p>
 * 提供评测题集版本管理、金标准用例标注、资料快照锁定、
 * 运行成本估算、批次评测启动与取消、明细追溯、人工复核判定以及多实验消融对比分析接口。
 */
@RestController
@RequestMapping("/api/v1/admin/eval")
public class EvaluationController {
    private final AccessPolicy access;
    private final EvaluationCatalog catalog;
    private final EvaluationRunner runner;

    /**
     * 构造评测控制器并注入权限策略、评测资产目录与执行器。
     *
     * @param access 访问控制策略
     * @param catalog 评测资产目录服务
     * @param runner 评测调度执行器
     */
    public EvaluationController(AccessPolicy access, EvaluationCatalog catalog, EvaluationRunner runner) {
        this.access = access;
        this.catalog = catalog;
        this.runner = runner;
    }

    /**
     * 查询租户内所有的评测题集版本列表。
     *
     * @param r HTTP 请求上下文
     * @return 题集版本列表
     */
    @GetMapping("/datasets")
    public Object datasets(HttpServletRequest r) {
        return catalog.datasets(access.actor(r));
    }

    /**
     * 创建新的评测题集版本（初始化为草稿 DRAFT 状态）。
     *
     * @param r HTTP 请求上下文
     * @param body 请求体 Map（含 name）
     * @return 新建题集版本视图 Map
     */
    @PostMapping("/datasets")
    public Object createDataset(HttpServletRequest r, @RequestBody Map<String, Object> body) {
        return catalog.createDataset(access.actor(r), body);
    }

    /**
     * 基于指定已有版本派生或深拷贝创建新的题集版本。
     *
     * @param r HTTP 请求上下文
     * @param id 默认源版本 ID
     * @param body 请求体 Map（可选 sourceVersionId）
     * @return 新派生题集版本视图 Map
     */
    @PostMapping("/datasets/{id}/versions")
    public Object createVersion(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
        var a = access.actor(r);
        String sourceId = Json.str(body, "sourceVersionId");
        if (!sourceId.isBlank()) {
            return catalog.copy(a, sourceId);
        }
        return catalog.copy(a, id);
    }

    /**
     * 获取指定题集版本下的全部评测用例列表。
     *
     * @param r HTTP 请求上下文
     * @param versionId 题集版本 ID
     * @return 评测用例列表
     */
    @GetMapping("/dataset-versions/{versionId}/cases")
    public Object cases(HttpServletRequest r, @PathVariable String versionId) {
        return catalog.cases(access.actor(r), versionId);
    }

    /**
     * 批量导入或保存指定题集版本下的评测用例数据。
     *
     * @param r HTTP 请求上下文
     * @param versionId 题集版本 ID
     * @param body 包含单条或多条用例列表的请求体 Map
     * @return 保存结果摘要 Map
     */
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

    /**
     * 冻结指定题集版本为不可变基准（FROZEN）。
     *
     * @param r HTTP 请求上下文
     * @param versionId 题集版本 ID
     * @return 冻结后的题集版本视图 Map
     */
    @PostMapping("/dataset-versions/{versionId}/freeze")
    public Object freeze(HttpServletRequest r, @PathVariable String versionId) {
        return catalog.freeze(access.actor(r), versionId);
    }

    /**
     * 更新单个评测用例的配置或标注数据。
     *
     * @param r HTTP 请求上下文
     * @param id 用例 ID
     * @param body 待更新的字段 Map
     * @return 更新摘要 Map
     */
    @PatchMapping("/cases/{id}")
    public Object updateCase(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
        return catalog.update(access.actor(r), id, body);
    }

    /**
     * 删除草稿题集中的单个评测用例。
     *
     * @param r HTTP 请求上下文
     * @param id 用例 ID
     * @return 删除状态 Map (status=DELETED)
     */
    @DeleteMapping("/cases/{id}")
    public Object deleteCase(HttpServletRequest r, @PathVariable String id) {
        catalog.delete(access.actor(r), id);
        return params("status", "DELETED");
    }

    /**
     * 查询租户内所有已创建的评测资料快照列表。
     *
     * @param r HTTP 请求上下文
     * @return 评测资料快照列表
     */
    @GetMapping("/snapshots")
    public Object snapshots(HttpServletRequest r) {
        return catalog.snapshots(access.actor(r));
    }

    /**
     * 为选定的知识库文档集创建不可变基准评测快照。
     *
     * @param r HTTP 请求上下文
     * @param body 请求体 Map（含 name, documentIds, approvedSyntheticOrRedacted）
     * @return 新建快照详情 Map
     */
    @PostMapping("/snapshots")
    public Object createSnapshot(HttpServletRequest r, @RequestBody Map<String, Object> body) {
        return catalog.snapshot(access.actor(r), body);
    }

    /**
     * 评测批次执行前预估所需的模型调用量、预计耗时与费用预算。
     *
     * @param r HTTP 请求上下文
     * @param body 请求体 Map（含 datasetVersionId, snapshotId, configId）
     * @return 成本预算预估 Map
     */
    @PostMapping("/estimates")
    public Object estimate(HttpServletRequest r, @RequestBody Map<String, Object> body) {
        return runner.estimate(access.actor(r), body);
    }

    /**
     * 提交并启动批量自动化评测运行（支持幂等键防重发）。
     *
     * @param r HTTP 请求上下文
     * @param body 运行配置 Map（含 datasetVersionId, snapshotId, configId 等）
     * @param key 幂等键（Idempotency-Key）
     * @return 启动的评测运行详情 Map
     */
    @PostMapping("/runs")
    public Object start(HttpServletRequest r, @RequestBody Map<String, Object> body, @RequestHeader(value = "Idempotency-Key", required = false) String key) {
        String effectiveKey = (key != null && !key.isBlank()) ? key : UUID.randomUUID().toString();
        return runner.start(access.actor(r), body, effectiveKey);
    }

    /**
     * 获取指定评测运行的执行状态及聚合评测指标统计（MRR、Hit@K、MAP、忠实度等）。
     *
     * @param r HTTP 请求上下文
     * @param id 评测运行 ID
     * @return 评测运行汇总视图 Map
     */
    @GetMapping("/runs/{id}")
    public Object getRun(HttpServletRequest r, @PathVariable String id) {
        return runner.get(access.actor(r), id);
    }

    /**
     * 查询指定评测运行中各评测用例的明细结果与单项得分。
     *
     * @param r HTTP 请求上下文
     * @param id 评测运行 ID
     * @return 用例评测明细列表
     */
    @GetMapping("/runs/{id}/results")
    public Object runResults(HttpServletRequest r, @PathVariable String id) {
        return runner.results(access.actor(r), id);
    }

    /**
     * 取消正在执行排队中的评测运行。
     *
     * @param r HTTP 请求上下文
     * @param id 评测运行 ID
     * @return 取消后的评测运行详情 Map
     */
    @PostMapping("/runs/{id}/cancel")
    public Object cancelRun(HttpServletRequest r, @PathVariable String id) {
        return runner.cancel(access.actor(r), id);
    }

    /**
     * 导出评测运行的结果与详细指标明细（脱敏 JSONL 格式）。
     *
     * @param r HTTP 请求上下文
     * @param id 评测运行 ID
     * @param body 导出参数 Map
     * @return 包含导出文本或下载信息的响应 Map
     */
    @PostMapping("/runs/{id}/exports")
    public Object exportRun(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
        return runner.export(access.actor(r), id, body);
    }

    /**
     * 人工复核单条评测结果，覆盖大模型裁判或自动化计算的得分。
     *
     * @param r HTTP 请求上下文
     * @param id 评测单项结果 ID
     * @param body 复核请求体 Map（含 reviewerNotes, overrides 等）
     * @return 复核结果 Map
     */
    @PostMapping("/results/{id}/reviews")
    public Object reviewResult(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
        return runner.review(access.actor(r), id, body);
    }

    /**
     * 对比多次评测运行的聚合指标与策略表现差异（消融实验与回归分析）。
     *
     * @param r HTTP 请求上下文
     * @param runIds 逗号分隔的多个评测运行 ID
     * @return 多运行横向对比分析报告 Map
     */
    @GetMapping("/comparisons")
    public Object compare(HttpServletRequest r, @RequestParam String runIds) {
        var a = access.actor(r);
        List<String> ids = Arrays.stream(runIds.split(",")).map(String::trim).filter(s -> !s.isEmpty()).toList();
        return runner.compare(a, ids);
    }
}
