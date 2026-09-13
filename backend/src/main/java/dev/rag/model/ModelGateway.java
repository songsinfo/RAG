package dev.rag.model;

import com.openai.errors.OpenAIIoException;
import com.openai.errors.OpenAIInvalidDataException;
import com.openai.errors.OpenAIRetryableException;
import com.openai.errors.OpenAIServiceException;
import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import org.springframework.ai.chat.messages.*;
import org.springframework.ai.chat.metadata.Usage;
import org.springframework.ai.chat.prompt.Prompt;
import org.springframework.ai.content.Media;
import org.springframework.ai.embedding.EmbeddingRequest;
import org.springframework.ai.openai.*;
import org.springframework.ai.tool.ToolCallback;
import org.springframework.ai.tool.definition.ToolDefinition;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;
import org.springframework.util.MimeType;

import java.net.*;
import java.net.http.*;
import java.nio.charset.StandardCharsets;
import java.time.*;
import java.math.*;
import java.util.*;
import java.util.concurrent.Semaphore;

import static dev.rag.common.Db.params;

/**
 * 统一大语言模型与多模态模型调用网关。
 * <p>
 * <b>核心职责与架构机制：</b>
 * <ul>
 *   <li><b>多模型路由与适配：</b>提供 Embedding（向量化）、Chat（对话补全）、ChatTools（工具调用）、Rerank（重排序）及 Vision（视觉理解）的统一入口；</li>
 *   <li><b>严格本地并发控制：</b>采用公平信号量 {@link Semaphore} 将单节点在途模型并发严格限制为 3，避免压垮外部供应商接口；</li>
 *   <li><b>细粒度预算审计：</b>调用前通过 {@link BudgetAuthority#reserve} 锁定费用上界，并在调用结束后根据实际 Token 消耗核销多退少补；</li>
 *   <li><b>动态来源版本校验：</b>调用前通过 {@link AccessPolicy#guard} 二次校验引用的文档未发生版本变更（防幻觉与脏数据引用）；</li>
 *   <li><b>容灾重试机制：</b>针对 429 限流及 5xx 供应商临时故障，执行最多 3 次指数退避重试（100ms, 200ms...）。</li>
 * </ul>
 */
@Service
public class ModelGateway {
    private final Db db;
    private final BudgetService budgets;
    private final BudgetAuthority authority;
    private final AccessPolicy access;
    private final String base, key, chat, embedding, vision, judge, rerank, environment;
    private final int dimensions;
    private final Semaphore slots = new Semaphore(3, true);
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();

    @FunctionalInterface
    private interface ProviderCall {
        ProviderResponse send(Duration timeout);
    }

    private record ProviderResponse(Map<String, Object> body, String requestId) {}

    /**
     * 构造模型网关服务。
     *
     * @param db 统一数据库操作组件
     * @param budgets 本地预算服务
     * @param authority 集中式/本地预算授权门面
     * @param access 访问控制与行级鉴权策略
     * @param base 模型供应商 API 基础路径
     * @param key 模型调用认证密钥 API Key
     * @param chat 默认对话模型名称
     * @param embedding 文本向量模型名称
     * @param vision 多模态视觉模型名称
     * @param judge 评估与裁判模型名称
     * @param rerank 检索重排序模型名称
     * @param dimensions 向量输出维度（默认 1024）
     * @param environment 当前运行环境标识
     */
    public ModelGateway(Db db, BudgetService budgets, BudgetAuthority authority, AccessPolicy access, @Value("${rag.model.base-url:}") String base, @Value("${rag.model.api-key:}") String key, @Value("${rag.model.chat:}") String chat, @Value("${rag.model.embedding:}") String embedding, @Value("${rag.model.vision:}") String vision, @Value("${rag.model.judge:}") String judge, @Value("${rag.model.rerank:}") String rerank, @Value("${rag.model.embedding-dimensions:1024}") int dimensions, @Value("${rag.environment:demo}") String environment) {
        this.db = db;
        this.budgets = budgets;
        this.authority = authority;
        this.access = access;
        this.base = base.replaceAll("/$", "");
        this.key = key;
        this.chat = chat;
        this.embedding = embedding;
        this.vision = vision;
        this.judge = judge;
        this.rerank = rerank;
        this.dimensions = dimensions;
        this.environment = environment;
    }

    /**
     * 获取当前生效的向量模型名称。
     *
     * @return 向量模型标识字符串
     */
    public String embeddingModel() {
        return embedding;
    }

    /**
     * 获取当前向量模型的向量维度。
     *
     * @return 向量维度整数（如 1024）
     */
    public int dimensions() {
        return dimensions;
    }

    /**
     * 批量计算输入文本的向量表示（Embedding）。
     * <p>
     * <b>数据校验与规范：</b>
     * <ul>
     *   <li>输入批量限制：单次最多 100 条文本；</li>
     *   <li>顺序完整性校验：严格按返回的 index 重新排序并校验连续性；</li>
     *   <li>维度与数值校验：必须精确匹配配置维度 dimensions，每个维度数值必须为有效有限浮点数（非 NaN / 非无穷），且向量模长非零。</li>
     * </ul>
     *
     * @param actor 操作主体
     * @param ownerKind 调用归属类型（RUN 或 JOB）
     * @param ownerId 调用归属实体 ID
     * @param texts 待向量化的文本片段列表
     * @return 稠密浮点向量列表（外层列表顺序与输入文本完全对应）
     * @throws ApiException 若输入非法或模型返回数据结构不合规（502 INVALID_MODEL_RESPONSE）
     */
    @SuppressWarnings("unchecked")
    public List<List<Double>> embed(Actor actor, String ownerKind, String ownerId, List<String> texts) {
        if (texts.isEmpty() || texts.size() > 100)
            throw new ApiException(400, "INVALID_INPUT", "Embedding 批量大小无效");
        var body = params("model", embedding, "input", texts, "dimensions", dimensions);
        var result = call(actor, ownerKind, ownerId, "EMBEDDING", embedding, body, 0, timeout -> springEmbedding(texts, timeout));
        if (!(result.get("data") instanceof List<?> data) || data.size() != texts.size()) throw invalid();
        List<Map<String, Object>> rows = new ArrayList<>();
        for (Object item : (List<?>) result.get("data")) rows.add((Map<String, Object>) item);
        rows.sort(Comparator.comparingInt(r -> Json.integer(r, "index", -1)));
        List<List<Double>> out = new ArrayList<>();
        for (int i = 0; i < rows.size(); i++) {
            var row = rows.get(i);
            if (Json.integer(row, "index", -1) != i || !(row.get("embedding") instanceof List<?> v) || v.size() != dimensions)
                throw invalid();
            List<Double> vector = new ArrayList<>();
            double norm = 0;
            for (Object n : (List<?>) row.get("embedding")) {
                double value = ((Number) n).doubleValue();
                if (!Double.isFinite(value)) throw invalid();
                vector.add(value);
                norm += value * value;
            }
            if (norm == 0) throw invalid();
            out.add(vector);
        }
        return out;
    }

    /**
     * 发起标准对话补全请求（无外部工具调用）。
     *
     * @param actor 操作主体
     * @param ownerKind 归属类型（RUN 或 JOB）
     * @param ownerId 归属实体 ID
     * @param purpose 调用目的（如 CHAT, VISION, JUDGE 等）
     * @param messages 上下文对话消息列表（role, content 等）
     * @param maxOutputTokens 最大允许生成的 Token 数量
     * @return 包含 role 与 content 的模型回复消息 Map
     */
    public Map<String, Object> chat(Actor actor, String ownerKind, String ownerId, String purpose, List<Map<String, Object>> messages, int maxOutputTokens) {
        return chatTools(actor, ownerKind, ownerId, purpose, messages, maxOutputTokens, List.of());
    }

    /**
     * 发起支持函数/工具调用（Tool/Function Calling）的对话补全请求。
     * <p>
     * <b>路由规则：</b>
     * <ul>
     *   <li>purpose 为 VISION 时路由至视觉多模态模型；</li>
     *   <li>purpose 为 JUDGE 时路由至裁判/评估大模型；</li>
     *   <li>其他情况路由至标准 chat 大模型；</li>
     *   <li>强制固定 temperature=0，保障生成结果的一致性与可复现性。</li>
     * </ul>
     *
     * @param actor 操作主体
     * @param ownerKind 归属类型
     * @param ownerId 归属实体 ID
     * @param purpose 调用目的标识
     * @param messages 对话上下文消息列表
     * @param maxOutputTokens 最大生成 Token 限制
     * @param tools 允许大模型调用的工具描述定义列表
     * @return 模型响应消息 Map（可能包含 content 或 tool_calls）
     * @throws ApiException 若模型响应解析失败
     */
    @SuppressWarnings("unchecked")
    public Map<String, Object> chatTools(Actor actor, String ownerKind, String ownerId, String purpose, List<Map<String, Object>> messages, int maxOutputTokens, List<Map<String, Object>> tools) {
        String model = purpose.equals("VISION") ? vision : purpose.equals("JUDGE") ? judge : chat;
        var body = params("model", model, "messages", messages, "max_tokens", maxOutputTokens, "temperature", 0);
        if (!tools.isEmpty()) body.put("tools", tools);
        var result = call(actor, ownerKind, ownerId, purpose, model, body, maxOutputTokens, timeout -> springChat(model, messages, tools, maxOutputTokens, timeout));
        try {
            return (Map<String, Object>) ((Map<?, ?>) ((List<?>) result.get("choices")).getFirst()).get("message");
        } catch (Exception e) {
            throw invalid();
        }
    }

    /**
     * 对候选文档列表执行查询相关性重排序（Rerank）。
     * <p>
     * 调用供应商 {@code /rerank} 端点，根据用户 Query 评估各个候选文档的相关性分数。
     * 严格校验返回记录的索引完整性与分数的有效有限性。
     *
     * @param a 操作主体
     * @param runId 所属 QA 运行 ID
     * @param query 用户检索查询词
     * @param documents 候选文档文本列表
     * @return 与输入候选文档列表严格对齐的相关性得分列表（Double 浮点数，值越大越相关）
     * @throws ApiException 若模型响应缺失或格式非法
     */
    @SuppressWarnings("unchecked")
    public List<Double> rerank(Actor a, String runId, String query, List<String> documents) {
        var body = params("model", rerank, "query", query, "documents", documents, "top_n", documents.size(), "return_documents", false);
        var result = call(a, "RUN", runId, "RERANK", rerank, body, 0, timeout -> directHttp("/rerank", body, timeout));
        List<Double> scores = new ArrayList<>(Collections.nCopies(documents.size(), null));
        try {
            for (Object item : (List<?>) result.get("results")) {
                var row = (Map<String, Object>) item;
                int index = Json.integer(row, "index", -1);
                double score = ((Number) row.get("relevance_score")).doubleValue();
                if (index < 0 || index >= scores.size() || scores.get(index) != null || !Double.isFinite(score))
                    throw invalid();
                scores.set(index, score);
            }
            if (scores.contains(null)) throw invalid();
            return scores;
        } catch (RuntimeException e) {
            throw invalid();
        }
    }

    /**
     * 构造 502 统一模型协议响应异常。
     *
     * @return ApiException 实例
     */
    private static ApiException invalid() {
        return new ApiException(502, "INVALID_MODEL_RESPONSE", "模型协议响应无效");
    }

    private ProviderResponse springEmbedding(List<String> texts, Duration timeout) {
        try {
            var options = OpenAiEmbeddingOptions.builder()
                    .apiKey(key).baseUrl(base).model(embedding).dimensions(dimensions)
                    .timeout(timeout).maxRetries(0).build();
            var response = OpenAiEmbeddingModel.builder().options(options).build()
                    .call(new EmbeddingRequest(texts, options));
            List<Map<String, Object>> data = new ArrayList<>();
            for (var item : response.getResults()) {
                List<Double> vector = new ArrayList<>(item.getOutput().length);
                for (float value : item.getOutput()) vector.add((double) value);
                data.add(params("index", item.getIndex(), "embedding", vector));
            }
            var body = params("data", data);
            addUsage(body, response.getMetadata().getUsage());
            return new ProviderResponse(body, null);
        } catch (RuntimeException e) {
            throw providerFailure(e);
        }
    }

    private ProviderResponse springChat(String model, List<Map<String, Object>> messages, List<Map<String, Object>> tools, int maxOutputTokens, Duration timeout) {
        try {
            var options = OpenAiChatOptions.builder()
                    .apiKey(key).baseUrl(base).model(model).temperature(0d).maxTokens(maxOutputTokens)
                    .toolCallbacks(toolCallbacks(tools)).parallelToolCalls(false)
                    .timeout(timeout).maxRetries(0).build();
            var response = OpenAiChatModel.builder().options(options).build()
                    .call(new Prompt(aiMessages(messages), options));
            var output = response.getResult().getOutput();
            var message = params("role", "assistant", "content", Objects.toString(output.getText(), ""));
            if (output.hasToolCalls()) {
                List<Map<String, Object>> calls = new ArrayList<>();
                for (var call : output.getToolCalls())
                    calls.add(params("id", call.id(), "type", call.type(), "function", params("name", call.name(), "arguments", call.arguments())));
                message.put("tool_calls", calls);
            }
            var body = params("choices", List.of(params("message", message)));
            addUsage(body, response.getMetadata().getUsage());
            return new ProviderResponse(body, response.getMetadata().getId());
        } catch (RuntimeException e) {
            throw providerFailure(e);
        }
    }

    private List<Message> aiMessages(List<Map<String, Object>> messages) {
        List<Message> converted = new ArrayList<>();
        Map<String, String> toolNames = new HashMap<>();
        for (var source : messages) {
            String role = Objects.toString(source.get("role"), "");
            switch (role) {
                case "system" -> converted.add(new SystemMessage(text(source.get("content"))));
                case "user" -> converted.add(userMessage(source.get("content")));
                case "assistant" -> {
                    List<AssistantMessage.ToolCall> calls = new ArrayList<>();
                    if (source.get("tool_calls") instanceof List<?> raw) {
                        for (Object item : raw) {
                            var call = castMap(item);
                            var function = castMap(call.get("function"));
                            String id = text(call.get("id")), name = text(function.get("name"));
                            toolNames.put(id, name);
                            calls.add(new AssistantMessage.ToolCall(id, text(call.getOrDefault("type", "function")), name, text(function.get("arguments"))));
                        }
                    }
                    converted.add(AssistantMessage.builder().content(text(source.get("content"))).toolCalls(calls).build());
                }
                case "tool" -> {
                    String id = text(source.get("tool_call_id"));
                    var response = new ToolResponseMessage.ToolResponse(id, toolNames.getOrDefault(id, ""), text(source.get("content")));
                    converted.add(ToolResponseMessage.builder().responses(List.of(response)).build());
                }
                default -> throw new ApiException(400, "INVALID_INPUT", "模型消息 role 无效");
            }
        }
        return converted;
    }

    private UserMessage userMessage(Object content) {
        if (!(content instanceof List<?> parts)) return new UserMessage(text(content));
        StringBuilder text = new StringBuilder();
        List<Media> media = new ArrayList<>();
        for (Object item : parts) {
            var part = castMap(item);
            if ("text".equals(part.get("type"))) text.append(text(part.get("text")));
            else if ("image_url".equals(part.get("type"))) media.add(media(text(castMap(part.get("image_url")).get("url"))));
            else throw new ApiException(400, "INVALID_INPUT", "不支持的多模态消息类型");
        }
        return UserMessage.builder().text(text.toString()).media(media).build();
    }

    private static Media media(String source) {
        try {
            if (!source.startsWith("data:")) return Media.builder().data(URI.create(source)).build();
            int comma = source.indexOf(',');
            if (comma < 0 || !source.substring(0, comma).endsWith(";base64")) throw new IllegalArgumentException();
            String mime = source.substring(5, source.indexOf(';'));
            return Media.builder().mimeType(MimeType.valueOf(mime)).data(Base64.getDecoder().decode(source.substring(comma + 1))).build();
        } catch (RuntimeException e) {
            throw new ApiException(400, "INVALID_INPUT", "图片数据无效");
        }
    }

    private static List<ToolCallback> toolCallbacks(List<Map<String, Object>> tools) {
        List<ToolCallback> callbacks = new ArrayList<>();
        for (var item : tools) {
            var function = castMap(item.get("function"));
            var definition = ToolDefinition.builder()
                    .name(text(function.get("name")))
                    .description(text(function.get("description")))
                    .inputSchema(Json.write(function.get("parameters")))
                    .build();
            callbacks.add(new ToolCallback() {
                @Override public ToolDefinition getToolDefinition() { return definition; }
                @Override public String call(String input) { throw new IllegalStateException("工具由业务 Agent 循环执行"); }
            });
        }
        return callbacks;
    }

    private ProviderResponse directHttp(String path, Map<String, Object> body, Duration timeout) {
        var request = HttpRequest.newBuilder(URI.create(base + path)).timeout(timeout)
                .header("Authorization", "Bearer " + key).header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(Json.write(body))).build();
        try {
            var response = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 429 || response.statusCode() >= 500)
                throw new ApiException(502, "MODEL_RETRYABLE", "模型服务暂不可用");
            if (response.statusCode() < 200 || response.statusCode() >= 300)
                throw new ApiException(502, "MODEL_PROVIDER_ERROR", "模型供应商请求失败");
            return new ProviderResponse(Json.object(response.body()), response.headers().firstValue("x-request-id").orElse(null));
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ApiException(503, "MODEL_TIMEOUT", "模型请求中断");
        } catch (ApiException e) {
            throw e;
        } catch (Exception e) {
            throw new ApiException(503, "MODEL_TIMEOUT", "模型请求超时或连接失败");
        }
    }

    private static void addUsage(Map<String, Object> body, Usage usage) {
        if (usage == null || usage.getPromptTokens() == null) return;
        body.put("usage", params("prompt_tokens", usage.getPromptTokens(), "completion_tokens", Objects.requireNonNullElse(usage.getCompletionTokens(), 0)));
    }

    private static ApiException providerFailure(RuntimeException failure) {
        if (failure instanceof ApiException api) return api;
        for (Throwable cause = failure; cause != null; cause = cause.getCause()) {
            if (cause instanceof OpenAIServiceException service) {
                if (service.statusCode() == 429 || service.statusCode() >= 500)
                    return new ApiException(502, "MODEL_RETRYABLE", "模型服务暂不可用");
                return new ApiException(502, "MODEL_PROVIDER_ERROR", "模型供应商请求失败");
            }
            if (cause instanceof OpenAIRetryableException)
                return new ApiException(502, "MODEL_RETRYABLE", "模型服务暂不可用");
            if (cause instanceof OpenAIIoException)
                return new ApiException(503, "MODEL_TIMEOUT", "模型请求超时或连接失败");
            if (cause instanceof OpenAIInvalidDataException)
                return invalid();
        }
        return new ApiException(502, "MODEL_PROVIDER_ERROR", "模型供应商响应处理失败");
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> castMap(Object value) {
        if (value instanceof Map<?, ?> map) return (Map<String, Object>) map;
        throw new ApiException(400, "INVALID_INPUT", "模型消息结构无效");
    }

    private static String text(Object value) {
        return Objects.toString(value, "");
    }

    /**
     * 校验 QA 运行引用的所有知识来源文档版本依然处于最新有效状态。
     * <p>
     * 避免在长链路对话或多轮调用期间，底层知识库文档已被更新或重新构建，造成基于过期版本回答。
     *
     * @param a 操作主体
     * @param owner QA 运行 runId
     * @throws ApiException 若运行已停止（409 CANCELLED）或文档已被重新构建（409 SOURCE_CHANGED）
     */
    private void verifyCurrentSources(Actor a, String owner) {
        var run = db.one("SELECT status FROM qa_run WHERE tenant_id=:t AND id=:id", params("t", a.tenantId(), "id", owner));
        if (!Set.of("QUEUED", "RUNNING").contains(run.get("status").toString()))
            throw new ApiException(409, "CANCELLED", "请求已停止");
        if (!db.list("SELECT s.id FROM run_source s JOIN document d ON d.tenant_id=s.tenant_id AND d.id=s.document_id WHERE s.tenant_id=:t AND s.run_id=:r AND s.purpose<>'HISTORY' AND (d.active_build_id IS NULL OR d.active_build_id<>s.build_id)", params("t", a.tenantId(), "r", owner)).isEmpty())
            throw new ApiException(409, "SOURCE_CHANGED", "来源版本已变化");
    }

    /**
     * 提取并校验后台任务（job）所关联的文档来源列表与生命周期屏障。
     *
     * @param a 操作主体
     * @param owner 后台作业 jobId
     * @param estimate 预估预算金额
     * @return 关联的文档 ID 列表
     * @throws ApiException 若任务已停止、来源版本生命周期发生突变或任务类型不合规
     */
    private List<String> jobSources(Actor a, String owner, BigDecimal estimate) {
        var job = db.one("SELECT * FROM job WHERE tenant_id=:t AND id=:id", params("t", a.tenantId(), "id", owner));
        if (!"RUNNING".equals(job.get("status")) || job.get("cancel_requested_at") != null)
            throw new ApiException(409, "CANCELLED", "后台任务已停止");
        String kind = Objects.toString(job.get("kind"), "");
        List<String> docs = new ArrayList<>();
        if (Set.of("INGEST", "RESTORE").contains(kind)) {
            var build = db.one("SELECT b.document_id,b.fence,d.lifecycle_epoch,d.status FROM index_build b JOIN document d ON d.tenant_id=b.tenant_id AND d.id=b.document_id WHERE b.tenant_id=:t AND b.id=:id", params("t", a.tenantId(), "id", job.get("target_id")));
            if (!"LIVE".equals(build.get("status")) || !Objects.equals(((Number) build.get("fence")).longValue(), ((Number) build.get("lifecycle_epoch")).longValue()))
                throw new ApiException(409, "SOURCE_CHANGED", "入库来源生命周期已变化");
            docs.add(build.get("document_id").toString());
        } else if (kind.equals("EVALUATE")) {
            for (var row : db.list("SELECT DISTINCT d.document_id FROM eval_run e JOIN eval_snapshot_document d ON d.tenant_id=e.tenant_id AND d.snapshot_id=e.snapshot_id WHERE e.tenant_id=:t AND e.job_id=:id UNION SELECT DISTINCT s.document_id FROM eval_result r JOIN eval_run e ON e.tenant_id=r.tenant_id AND e.id=r.eval_run_id JOIN run_source s ON s.tenant_id=r.tenant_id AND s.run_id=r.qa_run_id WHERE e.tenant_id=:t AND e.job_id=:id", params("t", a.tenantId(), "id", owner)))
                docs.add(row.get("document_id").toString());
        } else throw new ApiException(400, "INVALID_MODEL_OWNER", "此后台任务不允许调用模型");
        return docs;
    }

    /**
     * 执行大模型远程 HTTP 调用（附带最多 3 次有限重试与退避机制）。
     * <p>
     * <b>重试控制策略：</b>
     * <ul>
     *   <li>当捕获到错误码为 {@code MODEL_RETRYABLE} 的异常时（对应 HTTP 429 限流或 5xx 服务端故障），触发退避重试；</li>
     *   <li>重试休眠等待时间：attempt * 100ms（递增退避）；</li>
     *   <li>若连续 3 次重试仍未成功，抛出 502 MODEL_PROVIDER_ERROR 终止流程；</li>
     *   <li>非重试型异常（如参数错误 400、权限不足 403、文档变更 409 等）直接向上抛出，不作无意义重试。</li>
     * </ul>
     *
     * @param a 操作主体
     * @param kind 归属类型（RUN 问答会话 或 JOB 后台作业）
     * @param owner 归属实体 ID（runId 或 jobId）
     * @param purpose 调用业务目的（如 EMBEDDING, CHAT, RERANK, VISION, JUDGE）
     * @param model 实际请求调用的模型名称
     * @param body 请求体参数 Map
     * @param output 期望的最大输出 Token 数量（用于费用预估上限）
     * @param provider Spring AI 或供应商扩展端点调用
     * @return 解析后的模型响应 JSON Map
     * @throws ApiException 若调用失败且重试耗尽
     */
    private Map<String, Object> call(Actor a, String kind, String owner, String purpose, String model, Map<String, Object> body, int output, ProviderCall provider) {
        for (int attempt = 1; attempt <= 3; attempt++) {
            try {
                return callOnce(a, kind, owner, purpose, model, body, output, attempt, provider);
            } catch (ApiException e) {
                if (!e.code.equals("MODEL_RETRYABLE")) throw e;
                if (attempt == 3) throw new ApiException(502, "MODEL_PROVIDER_ERROR", "模型供应商有限重试仍失败");
                try {
                    Thread.sleep(attempt * 100L);
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    throw new ApiException(409, "CANCELLED", "调用重试已中断");
                }
            }
        }
        throw new IllegalStateException();
    }

    /**
     * 单次模型调用的核心执行流水线。
     * <p>
     * <b>完整生命周期管控步骤：</b>
     * <ol>
     *   <li><b>配置与入参预检：</b>校验 base-url、api-key 及模型非空；校验输入字节长度不超过 120,000 字节（粗粒度防御超长请求攻击）；</li>
     *   <li><b>公平信号量限流：</b>非阻塞尝试获取本地许可 {@code slots.tryAcquire()}，满载时直接返回 429 RATE_LIMITED 保护本地线程；</li>
     *   <li><b>会话/任务配额刚性检查：</b>
     *       <ul>
     *         <li>对于 RUN：校验会话处于 QUEUED/RUNNING；校验单会话调用次数上限（AGENT 模式 12 次，普通模式 8 次）；
     *             校验累计输入 Token（<= 30,000）与输出 Token（<= 6,000）；校验剩余超时时间（超时前必须完成）；</li>
     *         <li>对于 JOB：校验后台批次作业未被取消，并通过 {@link BudgetService#requireBatchRunning} 校验批次健康状态。</li>
     *       </ul>
     *   </li>
     *   <li><b>预算预扣与记账：</b>调用 {@link BudgetAuthority#reserve} 申请额度预扣，并在 {@code model_call} 表登记 RESERVED 调用流水；</li>
     *   <li><b>来源鉴权与网络发送：</b>在 {@link AccessPolicy#guard} 保护下二次确认引用的来源文档版本合法，发起 HTTP POST 请求；</li>
     *   <li><b>用量核销与对账：</b>
     *       <ul>
     *         <li>正常响应：解析 usage 字段中的真实 prompt_tokens 与 completion_tokens，按实际用量向预算中心结算 SETTLED，并更新流水表；</li>
     *         <li>无 usage 返回：向预算中心标记 UNKNOWN，保留预扣额供后续人工/补偿对账；</li>
     *         <li>异常补偿：若 HTTP 请求尚未发出（sent=false），结算标记为 NOT_SENT，释放全额预扣资金；若已发出（sent=true），标记为 UNKNOWN。</li>
     *       </ul>
     *   </li>
     *   <li><b>资源清理：</b>在 finally 块中绝对保证释放 Semaphore 信号量槽位。</li>
     * </ol>
     *
     * @param a 操作主体
     * @param kind 归属类型（RUN 或 JOB）
     * @param owner 归属实体 ID
     * @param purpose 调用目的标识
     * @param model 模型名称
     * @param body 请求负载 Map
     * @param output 最大输出 Token 限制
     * @param attempt 当前重试尝试轮次编号（1-3）
     * @param provider Spring AI 或供应商扩展端点调用
     * @return 响应结果 JSON Map
     * @throws ApiException 若预检不合规、配额超限、来源失效或网络调用异常
     */
    private Map<String, Object> callOnce(Actor a, String kind, String owner, String purpose, String model, Map<String, Object> body, int output, int attempt, ProviderCall provider) {
        if (base.isBlank() || key.isBlank() || model.isBlank())
            throw new ApiException(503, "MODEL_NOT_CONFIGURED", "未配置对应真实模型服务");
        if (!Set.of("RUN", "JOB").contains(kind) || output < 0 || output > 6000)
            throw new ApiException(400, "INVALID_INPUT", "模型调用参数无效");
        String json = Json.write(body);
        long input = json.getBytes(StandardCharsets.UTF_8).length; // Conservative bound, not a tokenizer claim.
        if (input > 120000) throw new ApiException(413, "INPUT_TOO_LARGE", "模型输入超过请求硬限制");
        if (!slots.tryAcquire()) throw new ApiException(429, "RATE_LIMITED", "模型在途并发已满");
        String callId = Json.id();
        BudgetAuthority.Reservation reservation = null;
        boolean sent = false;
        try {
            var prices = budgets.pricing(a);
            BigDecimal estimate = BudgetService.estimate(prices, input, output);
            List<String> docs = new ArrayList<>();
            Duration timeout = Duration.ofSeconds(90);
            String batchJobId = kind.equals("JOB") ? owner : null;
            if (kind.equals("RUN")) {
                var run = db.one("SELECT * FROM qa_run WHERE tenant_id=:t AND id=:id AND owner_id=:u", params("t", a.tenantId(), "id", owner, "u", a.userId()));
                if (!Set.of("QUEUED", "RUNNING").contains(run.get("status").toString()))
                    throw new ApiException(409, "CANCELLED", "请求已停止");
                int maxCalls = run.get("mode").equals("AGENT") ? 12 : 8;
                var usage = db.one("SELECT COUNT(*) AS calls,COALESCE(SUM(COALESCE(input_tokens,JSON_EXTRACT(usage_detail,'$.inputUpperBound'))),0) AS input_total,COALESCE(SUM(COALESCE(output_tokens,JSON_EXTRACT(usage_detail,'$.outputUpperBound'))),0) AS output_total FROM model_call WHERE tenant_id=:t AND run_id=:r", params("t", a.tenantId(), "r", owner));
                if (((Number) usage.get("calls")).intValue() >= maxCalls || new BigDecimal(usage.get("input_total").toString()).longValue() + input > 30000 || new BigDecimal(usage.get("output_total").toString()).longValue() + output > 6000)
                    throw new ApiException(429, "BUDGET_EXCEEDED", "请求全局调用或 token 上界已耗尽");
                var budget = Json.object(run.get("budget_json").toString());
                batchJobId = Objects.toString(budget.get("evaluationJobId"), null);
                long left = Json.integer(budget, "deadlineSeconds", run.get("mode").equals("AGENT") ? 60 : 45) * 1000L - (System.currentTimeMillis() - Long.parseLong(budget.getOrDefault("startedEpochMs", System.currentTimeMillis()).toString()));
                if (left <= 0) throw new ApiException(429, "BUDGET_EXCEEDED", "请求时限已耗尽");
                timeout = Duration.ofMillis(Math.min(45000, left));
                for (var r : db.list("SELECT DISTINCT document_id FROM run_source WHERE tenant_id=:t AND run_id=:r", params("t", a.tenantId(), "r", owner)))
                    docs.add(r.get("document_id").toString());
            } else {
                docs.addAll(jobSources(a, owner, estimate));
            }
            if (batchJobId != null) budgets.requireBatchRunning(a, batchJobId);
            reservation = authority.reserve(a, callId, input, output, prices);
            estimate = reservation.amount();
            var callRow = params("tenant_id", a.tenantId(), "id", callId, "run_id", kind.equals("RUN") ? owner : null, "job_id", kind.equals("JOB") ? owner : null, "purpose", purpose, "model_config_id", prices.get("configId"), "attempt_no", attempt, "status", "RESERVED", "reserved_amount", estimate, "pricing_snapshot", Json.write(reservation.price()), "usage_detail", Json.write(params("inputUpperBound", input, "outputUpperBound", output)), "reservation_authority", reservation.authority(), "reservation_id", reservation.id());
            if (batchJobId == null) db.insert("model_call", callRow);
            else budgets.recordBatchCall(a, batchJobId, callRow);
            // Authority gate includes every source sent previously, including rerank and history dependencies.
            final boolean[] dispatched = {false};
            final String linkedBatch = batchJobId;
            final Duration callTimeout = timeout;
            ProviderResponse response;
            try {
                response = access.guard(a, docs, () -> {
                    if (kind.equals("RUN")) verifyCurrentSources(a, owner);
                    else jobSources(a, owner, BigDecimal.ZERO);
                    if (linkedBatch != null) budgets.requireBatchRunning(a, linkedBatch);
                    dispatched[0] = true;
                    return provider.send(callTimeout);
                });
            } finally {
                sent = dispatched[0];
            }
            var result = response.body();
            Object usageRaw = result.get("usage");
            if (usageRaw instanceof Map<?, ?> usage && usage.get("prompt_tokens") instanceof Number in) {
                long out = usage.get("completion_tokens") instanceof Number n ? n.longValue() : 0;
                BigDecimal actual = BudgetService.estimate(reservation.price(), in.longValue(), out);
                authority.settle(a, reservation, "SETTLED", in.longValue(), out);
                db.exec("UPDATE model_call SET status='SETTLED',settled_amount=:a,input_tokens=:i,output_tokens=:o,provider_request_id=:p,usage_detail=:d WHERE tenant_id=:t AND id=:id", params("a", actual, "i", in.longValue(), "o", out, "p", response.requestId(), "d", Json.write(usage), "t", a.tenantId(), "id", callId));
            } else {
                authority.settle(a, reservation, "UNKNOWN", null, null);
                db.exec("UPDATE model_call SET status='UNKNOWN' WHERE tenant_id=:t AND id=:id", params("t", a.tenantId(), "id", callId));
            }
            if (kind.equals("RUN"))
                db.exec("UPDATE run_source SET send_status='SENT' WHERE tenant_id=:t AND run_id=:id AND send_status='PREPARED'", params("t", a.tenantId(), "id", owner));
            return result;
        } catch (RuntimeException e) {
            if (reservation != null) {
                try {
                    authority.settle(a, reservation, sent ? "UNKNOWN" : "NOT_SENT", null, null);
                } catch (RuntimeException reconciliation) {
                    e.addSuppressed(reconciliation);
                }
                db.exec("UPDATE model_call SET status=:s,settled_amount=CASE WHEN :s='NOT_SENT' THEN 0 ELSE settled_amount END WHERE tenant_id=:t AND id=:id AND status='RESERVED'", params("s", sent ? "UNKNOWN" : "NOT_SENT", "t", a.tenantId(), "id", callId));
            }
            throw e;
        } finally {
            slots.release();
        }
    }
}
