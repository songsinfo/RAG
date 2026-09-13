package dev.rag.qa;

import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import dev.rag.search.SearchService;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.web.bind.annotation.*;
import org.springframework.http.ResponseEntity;

import java.util.*;

import static dev.rag.common.Db.params;

/**
 * 智能知识问答与多轮会话 REST 控制器。
 * <p>
 * 提供多轮会话管理、上下文分段切换、异步问答提交（支持幂等控制）、
 * 运行状态轮询、运行取消、证据追溯、用户反馈提交以及管理员链路追踪与反馈处理接口。
 */
@RestController
@RequestMapping("/api/v1")
public class QaController {
    private final AccessPolicy access;
    private final QaService qa;
    private final SearchService search;
    private final Db db;

    /**
     * 构造问答控制器并注入安全策略、问答服务、检索服务与数据库客户端。
     *
     * @param access 访问控制策略
     * @param qa 问答服务
     * @param search 检索服务
     * @param db 数据库客户端
     */
    public QaController(AccessPolicy access, QaService qa, SearchService search, Db db) {
        this.access = access;
        this.qa = qa;
        this.search = search;
        this.db = db;
    }

    /**
     * 创建新的问答会话（同时初始化首个默认上下文分段）。
     *
     * @param request HTTP 请求上下文
     * @param body 请求体（可选 title, scopeMode, kbIds, configId）
     * @return 新建会话详情视图
     */
    @PostMapping("/conversations")
    public Object create(HttpServletRequest request, @RequestBody Map<String, Object> body) {
        return qa.createConversation(access.actor(request), body);
    }

    /**
     * 查询当前用户在当前租户下所有未删除的问答会话列表。
     *
     * @param r HTTP 请求上下文
     * @return 会话摘要列表（按活跃时间降序）
     */
    @GetMapping("/conversations")
    public Object list(HttpServletRequest r) {
        return qa.conversations(access.actor(r));
    }

    /**
     * 查询指定会话的详细信息（包括当前活跃分段及历史运行轮次列表）。
     *
     * @param r HTTP 请求上下文
     * @param id 会话 ID
     * @return 会话详情视图 Map
     */
    @GetMapping("/conversations/{id}")
    public Object detail(HttpServletRequest r, @PathVariable String id) {
        return qa.conversationDetail(access.actor(r), id);
    }

    /**
     * 逻辑删除指定会话并对关联运行的问答正文进行隐私脱敏。
     *
     * @param r HTTP 请求上下文
     * @param id 会话 ID
     * @return 删除状态 Map (status=DELETED)
     */
    @DeleteMapping("/conversations/{id}")
    public Object delete(HttpServletRequest r, @PathVariable String id) {
        qa.deleteConversation(access.actor(r), id);
        return params("status", "DELETED");
    }

    /**
     * 在指定会话中开启新的上下文分段（用于切换知识库检索范围或重置对话配置）。
     *
     * @param r HTTP 请求上下文
     * @param id 会话 ID
     * @param body 分段配置（scopeMode, kbIds, configId）
     * @return 新分段详情 Map
     */
    @PostMapping("/conversations/{id}/segments")
    public Object segment(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
        return qa.newSegment(access.actor(r), id, body);
    }

    /**
     * 提交新的问答请求并启动后台异步生成流程。
     * <p>
     * 支持 HTTP {@code Idempotency-Key} 头进行幂等去重防重发；成功接收返回 HTTP 202 Accepted。
     *
     * @param r HTTP 请求上下文
     * @param id 会话 ID
     * @param body 运行参数 Map（含 question, mode, strictMode 等）
     * @param key 可选的幂等键（Idempotency-Key）
     * @return 包含 runId 与状态的响应体（HTTP 202）
     */
    @PostMapping("/conversations/{id}/runs")
    public ResponseEntity<?> submit(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body, @RequestHeader(value = "Idempotency-Key", required = false) String key) {
        return ResponseEntity.accepted().body(qa.submit(access.actor(r), id, body, key));
    }

    /**
     * 获取指定问答运行的结果详情与执行阶段状态。
     *
     * @param r HTTP 请求上下文
     * @param id 问答运行 ID
     * @return 运行视图详情 Map（包含回答、证据、警告、状态等）
     */
    @GetMapping("/runs/{id}")
    public Object get(HttpServletRequest r, @PathVariable String id) {
        return qa.get(access.actor(r), id);
    }

    /**
     * 取消正在排队或执行中的问答运行。
     *
     * @param r HTTP 请求上下文
     * @param id 问答运行 ID
     * @return 取消后的运行详情 Map
     */
    @PostMapping("/runs/{id}/cancel")
    public Object cancel(HttpServletRequest r, @PathVariable String id) {
        return qa.cancel(access.actor(r), id);
    }

    /**
     * 提交或修改针对某次问答结果的用户评价反馈。
     *
     * @param r HTTP 请求上下文
     * @param id 问答运行 ID
     * @param body 反馈体 Map（含 rating, issueType, comment）
     * @return 反馈保存状态 Map
     */
    @PutMapping("/runs/{id}/feedback")
    public Object feedback(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
        return qa.feedback(access.actor(r), id, body);
    }

    /**
     * 根据证据编号（evidenceId）查询单次问答中引用的具体切片信息。
     *
     * @param r HTTP 请求上下文
     * @param id 问答运行 ID
     * @param evidenceId 证据编号（如 E1, E2）
     * @return 匹配的证据切片详情 Map
     */
    @GetMapping("/runs/{id}/evidence/{evidenceId}")
    public Object evidence(HttpServletRequest r, @PathVariable String id, @PathVariable String evidenceId) {
        var result = qa.get(access.actor(r), id);
        if (!(result.get("evidence") instanceof List<?> list)) throw ApiException.missing();
        return list.stream().filter(e -> Objects.equals(((Map<?, ?>) e).get("evidenceId"), evidenceId)).findFirst().orElseThrow(ApiException::missing);
    }

    /**
     * 管理员查询指定问答运行的全流程阶段执行事件流水（链路追踪）。
     *
     * @param r HTTP 请求上下文
     * @param id 问答运行 ID
     * @return 运行轨迹 Map
     */
    @GetMapping("/admin/runs/{id}/trace")
    public Object trace(HttpServletRequest r, @PathVariable String id) {
        return qa.trace(access.actor(r), id);
    }

    /**
     * 即席知识库检索测试接口（BM25 检索模式）。
     *
     * @param r HTTP 请求上下文
     * @param query 查询词
     * @param kbIds 可选限定的知识库 ID 列表
     * @return 检索结果上下文
     */
    @GetMapping("/search")
    public Object search(HttpServletRequest r, @RequestParam String query, @RequestParam(required = false) List<String> kbIds) {
        var a = access.actor(r);
        return search.search(a, null, query, kbIds == null ? List.of() : kbIds, Map.of("retrieval", Map.of("mode", "BM25")), List.of());
    }

    /**
     * 管理员查询租户内最近提交的用户问答反馈列表（最多 100 条）。
     *
     * @param r HTTP 请求上下文
     * @return 反馈摘要列表
     */
    @GetMapping("/admin/feedback")
    public Object feedbackList(HttpServletRequest r) {
        var a = access.actor(r);
        access.requireAdmin(a);
        List<Map<String, Object>> out = new ArrayList<>();
        for (var row : db.list("SELECT * FROM feedback WHERE tenant_id=:t ORDER BY created_at DESC LIMIT 100", params("t", a.tenantId()))) {
            try {
                qa.trace(a, row.get("run_id").toString());
                out.add(feedbackView(row));
            } catch (ApiException e) {
                if (e.status != 404) throw e;
                out.add(params("id", row.get("id"), "status", row.get("status"), "visibility", "HIDDEN"));
            }
        }
        return out;
    }

    /**
     * 管理员查询单个反馈记录的详细信息。
     *
     * @param r HTTP 请求上下文
     * @param id 反馈记录 ID
     * @return 反馈详情视图 Map
     */
    @GetMapping("/admin/feedback/{id}")
    public Object feedbackDetail(HttpServletRequest r, @PathVariable String id) {
        var a = access.actor(r);
        access.requireAdmin(a);
        var row = db.one("SELECT * FROM feedback WHERE tenant_id=:t AND id=:id", params("t", a.tenantId(), "id", id));
        qa.trace(a, row.get("run_id").toString());
        return feedbackView(row);
    }

    /**
     * 管理员更新反馈的处理状态与解决方案记录。
     *
     * @param r HTTP 请求上下文
     * @param id 反馈记录 ID
     * @param body 请求体 Map（含 status: OPEN|IN_PROGRESS|RESOLVED, resolution）
     * @return 更新后的状态 Map
     */
    @PatchMapping("/admin/feedback/{id}")
    public Object feedbackUpdate(HttpServletRequest r, @PathVariable String id, @RequestBody Map<String, Object> body) {
        var a = access.actor(r);
        feedbackDetail(r, id);
        String status = Json.str(body, "status"), resolution = Json.str(body, "resolution");
        if (!Set.of("OPEN", "IN_PROGRESS", "RESOLVED").contains(status) || resolution.length() > 4000)
            throw new ApiException(400, "INVALID_INPUT", "处理参数无效");
        db.exec("UPDATE feedback SET status=:s,resolution_text=:x WHERE tenant_id=:t AND id=:id", params("s", status, "x", resolution, "t", a.tenantId(), "id", id));
        return params("status", status);
    }

    /**
     * 将反馈数据行格式化为外部视图 Map。
     *
     * @param r 数据库查询行 Map
     * @return 格式化后的反馈视图 Map
     */
    private Map<String, Object> feedbackView(Map<String, Object> r) {
        return params("id", r.get("id"), "runId", r.get("run_id"), "rating", r.get("rating"), "issueType", r.get("issue_type"), "comment", r.get("comment_text"), "status", r.get("status"), "resolution", r.get("resolution_text"));
    }
}
