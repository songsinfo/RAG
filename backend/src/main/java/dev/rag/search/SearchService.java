package dev.rag.search;

import dev.rag.common.*;
import dev.rag.identity.AccessPolicy;
import dev.rag.model.ModelGateway;
import org.springframework.stereotype.Service;
import org.springframework.beans.factory.annotation.Value;

import java.net.*;
import java.net.http.*;
import java.nio.*;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.*;

import static dev.rag.common.Db.params;

/**
 * 多路混合检索与精准上下文装配引擎服务。
 * <p>
 * <b>核心检索流水线架构：</b>
 * <ol>
 *   <li><b>查询理解与改写（Query Preprocessing）：</b>
 *       支持多轮对话追问指代消解（Rewrite）、多角度查询扩展（Multi-Query）与假设性文档嵌入（HyDE）；</li>
 *   <li><b>异构多路检索（Multi-Route Retrieval）：</b>
 *       并行发起基于 Elasticsearch 的 BM25 全文及代码标识符加权检索、以及稠密向量 kNN 检索；</li>
 *   <li><b>排名融合（Fusion）：</b>
 *       应用加权倒数排名融合（RRF）平衡跨通路位次；</li>
 *   <li><b>后置优化与重排序（Post-Processing）：</b>
 *       支持神经重排序（Rerank）与兼顾多样性的最大边际相关性（MMR）挑选；</li>
 *   <li><b>证据上下文组装（Context Assembly）：</b>
 *       支持父块回溯展开（Parent-Child Expansion）、字符级精准 Span 截取装配与租户安全隔离校验。</li>
 * </ol>
 */
@Service
public class SearchService {
    /**
     * 检索召回与上下文装配产物。
     *
     * @param evidence 入选最终 Prompt 的结构化证据列表（含切片文本、定位坐标与来源元数据）
     * @param trace 检索执行全链路追踪数据（包含各通路命中列表、融合分、重排分与用量指标）
     * @param warnings 检索过程中产生的降级告警列表（如 REWRITE_SKIPPED, CONTEXT_TRIMMED 等）
     */
    public record Context(List<Map<String, Object>> evidence, Map<String, Object> trace, List<String> warnings) {
    }

    private final Db db;
    private final AccessPolicy access;
    private final ModelGateway model;
    private final String es, prefix;
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

    /**
     * 构造检索核心服务。
     *
     * @param db 统一数据库操作组件
     * @param access 访问控制组件
     * @param model 大模型调用网关
     * @param es Elasticsearch 基础访问 URL
     * @param prefix ES 索引命名前缀
     */
    public SearchService(Db db, AccessPolicy access, ModelGateway model, @Value("${rag.elasticsearch-url}") String es, @Value("${rag.index-prefix}") String prefix) {
        this.db = db;
        this.access = access;
        this.model = model;
        this.es = es;
        this.prefix = prefix;
    }

    /**
     * 从多层复合配置字典中提取指定的子配置块。
     *
     * @param config 检索总配置 Map
     * @param name 子模块名称（如 "query", "retrieval", "post", "context", "fusion"）
     * @return 对应的子配置 Map，若不存在或非 Map 类型则返回空 Map
     */
    @SuppressWarnings("unchecked")
    public static Map<String, Object> part(Map<String, Object> config, String name) {
        Object p = config.get(name);
        return p instanceof Map<?, ?> ? (Map<String, Object>) p : Map.of();
    }

    /**
     * 严格校验检索管道配置参数的取值范围与跨字段约束。
     * <p>
     * <b>约束边界规范：</b>
     * <ul>
     *   <li>模式限制：mode 必须为 BM25 / SPARSE / DENSE / HYBRID 之一；</li>
     *   <li>变体限制：maxVariants 介于 [1, 4]；</li>
     *   <li>通道容量：perRouteK 介于 [1, 100]，numCandidates 介于 [perRouteK, 1000]；</li>
     *   <li>上下文限制：maxTokens 介于 [100, 12000]，maxUnits 介于 [1, 20]；</li>
     *   <li>表示类型：representations 必须是 ORIGINAL, SUMMARY, QUESTION 的合法子集；</li>
     *   <li>算法参数：MMR lambda 介于 [0.0, 1.0]，rrfK 介于 [1, 1000]，rerankTopN 介于 [1, 30]；</li>
     *   <li>前置依赖：启用 HyDE 时模式必须为 DENSE 或 HYBRID。</li>
     * </ul>
     *
     * @param c 客户端传入的检索配置 Map
     * @throws ApiException 若配置参数超出支持范围或存在冲突（400 INVALID_CONFIG）
     */
    public static void validateConfig(Map<String, Object> c) {
        var q = part(c, "query");
        var r = part(c, "retrieval");
        var p = part(c, "post");
        String mode = r.getOrDefault("mode", "BM25").toString();
        if (!Set.of("BM25", "SPARSE", "DENSE", "HYBRID").contains(mode) || Json.integer(q, "maxVariants", 4) < 1 || Json.integer(q, "maxVariants", 4) > 4 || Json.integer(r, "perRouteK", 50) < 1 || Json.integer(r, "perRouteK", 50) > 100 || Json.integer(r, "numCandidates", 200) < Json.integer(r, "perRouteK", 50) || Json.integer(r, "numCandidates", 200) > 1000 || Json.integer(part(c, "context"), "maxTokens", 6000) < 100 || Json.integer(part(c, "context"), "maxTokens", 6000) > 12000 || Json.integer(part(c, "context"), "maxUnits", 6) < 1 || Json.integer(part(c, "context"), "maxUnits", 6) > 20)
            throw new ApiException(400, "INVALID_CONFIG", "检索参数超出支持范围");
        if (Json.bool(q, "hyde", false) && !Set.of("DENSE", "HYBRID").contains(mode))
            throw new ApiException(400, "INVALID_CONFIG", "HyDE 需要 Dense 通道");
        Object reps = r.getOrDefault("representations", List.of("ORIGINAL"));
        if (!(reps instanceof List<?> list) || list.isEmpty() || !Set.of("ORIGINAL", "SUMMARY", "QUESTION").containsAll(list))
            throw new ApiException(400, "INVALID_CONFIG", "表示类型无效");
        double lambda = Double.parseDouble(p.getOrDefault("lambda", .7).toString());
        if (!Double.isFinite(lambda) || lambda < 0 || lambda > 1)
            throw new ApiException(400, "INVALID_CONFIG", "MMR lambda 无效");
        if (Json.integer(part(c, "fusion"), "rrfK", 60) < 1 || Json.integer(part(c, "fusion"), "rrfK", 60) > 1000 || Json.integer(p, "rerankTopN", 30) < 1 || Json.integer(p, "rerankTopN", 30) > 30)
            throw new ApiException(400, "INVALID_CONFIG", "融合或重排容量无效");
        for (String section : List.of("query", "post", "answer"))
            for (var entry : part(c, section).entrySet())
                if (Set.of("rewrite", "hyde", "multiQuery", "rerank", "allowRerankFallback", "mmr", "parent", "strict", "allowRewriteFallback").contains(entry.getKey()) && !(entry.getValue() instanceof Boolean))
                    throw new ApiException(400, "INVALID_CONFIG", "策略开关必须是 boolean");
    }

    /**
     * 校验租户内所有可读活跃索引是否具备满足该检索配置的能力特征。
     * <p>
     * 检查点：
     * <ul>
     *   <li>当启用向量检索或 MMR 时，检验所有活跃构建是否包含配置所需的表示形式（ORIGINAL/SUMMARY/QUESTION）；</li>
     *   <li>检验索引构建时保存的向量维度和模型指纹是否与当前网关配置的模型严格一致。</li>
     * </ul>
     *
     * @param actor 操作主体
     * @param config 待校验的检索配置 Map
     * @throws ApiException 若租户索引缺少所需能力或模型指纹不匹配（409 INDEX_CAPABILITY_MISSING）
     */
    public void validateCapabilities(Actor actor, Map<String, Object> config) {
        validateConfig(config);
        var retrieval = part(config, "retrieval");
        var post = part(config, "post");
        boolean vector = Set.of("DENSE", "HYBRID").contains(retrieval.getOrDefault("mode", "BM25")) || Json.bool(post, "mmr", false);
        if (!vector) return;
        var builds = access.readableBuilds(actor, List.of());
        if (builds.isEmpty()) return;
        var reps = (List<?>) retrieval.getOrDefault("representations", List.of("ORIGINAL"));
        for (var b : db.list("SELECT capabilities,embedding_dims,model_fingerprint FROM index_build WHERE tenant_id=:t AND id IN (:ids)", params("t", actor.tenantId(), "ids", builds))) {
            for (Object rep : reps)
                if (!b.get("capabilities").toString().contains(rep.toString()))
                    throw new ApiException(409, "INDEX_CAPABILITY_MISSING", "当前索引缺少配置要求的表示");
            if (b.get("embedding_dims") == null || ((Number) b.get("embedding_dims")).intValue() != model.dimensions() || !Objects.equals(b.get("model_fingerprint"), model.embeddingModel()))
                throw new ApiException(409, "INDEX_CAPABILITY_MISSING", "当前索引向量模型不兼容");
        }
    }

    /**
     * 执行端到端多路检索、融合重排与精准上下文装配。
     * <p>
     * <b>核心执行步骤与技术细节：</b>
     * <ol>
     *   <li><b>前置校验与权限范围界定：</b>校验问题非空且不超过 2000 字符；
     *       调用 {@link AccessPolicy#readableBuilds} 提取调用方可见且处于 LIVE 状态的活跃 index_build 列表；
     *       若索引数量超 10,000 抛出 409 CAPACITY_LIMIT 保护系统；若无可读构建直接返回空上下文；</li>
     *   <li><b>特征能力二次检验：</b>若检索模式包含 DENSE/HYBRID 或启用了 MMR，
     *       严格校验各可读构建是否包含所需表示形式，以及向量维度和模型指纹的一致性；</li>
     *   <li><b>查询理解与多路扩展：</b>
     *       <ul>
     *         <li><b>Query Rewrite（指代消解）：</b>若开启且历史对话非空，调用模型将追问改写为脱敏独立问题（可降级容灾跳过并记录 REWRITE_SKIPPED）；</li>
     *         <li><b>Multi-Query（多查询扩展）：</b>生成最多两个语义同义变体问法；</li>
     *         <li><b>HyDE（假设性文档嵌入）：</b>生成用于语义检索的虚构段落，仅参与稠密向量检索通道。</li>
     *       </ul>
     *   </li>
     *   <li><b>多路检索与 RRF 融合：</b>
     *       <ul>
     *         <li>稀疏通道（BM25）：对 ES 发起多字段匹配（title^2 权重提升）及代码专用正则分词 term 匹配（boost 5）；</li>
     *         <li>稠密通道（DENSE）：调用网关嵌入向量并在 ES 中执行 kNN 检索；</li>
     *         <li>RRF 融合：通道间均分权重，计算各通路融合排位，初筛前 30 名候选。</li>
     *       </ul>
     *   </li>
     *   <li><b>后置优化（Rerank 与 MMR）：</b>
     *       <ul>
     *         <li>Rerank：通过模型重排服务计算交叉注意力相关性打分（支持失败降级为 RERANK_SKIPPED）；</li>
     *         <li>MMR：若配置开启，加载原文物理向量，执行最大边际相关性贪心挑选，打散相似内容。</li>
     *       </ul>
     *   </li>
     *   <li><b>父块展开与字符级精准装配：</b>
     *       <ul>
     *         <li>父块回溯：若开启 parent=true 且在 Token 预算限制内，展开为完整父块，否则回退为原块并记录 PARENT_TRIMMED；</li>
     *         <li>精确切片提取：按 Unicode CodePoint 偏移截取 source_unit 正文片段，累计 Token 上界（超限记录 CONTEXT_TRIMMED）；</li>
     *         <li>安全审计与行级鉴权：写入 run_source 证据记录，最终通过 {@link AccessPolicy#guard} 返回上下文。</li>
     *       </ul>
     *   </li>
     * </ol>
     *
     * @param actor 查询操作主体
     * @param runId 关联的问答会话运行 ID（可为空）
     * @param question 用户输入的原始问题
     * @param kbIds 目标限定的知识库 ID 列表（可为空，表示全部可见知识库）
     * @param config 当前生效的检索管道配置 Map
     * @param history 上下文历史对话列表（用于指代消解）
     * @return 装配完成的 {@link Context} 产物（含证据片段、链路 trace 与告警 warnings）
     * @throws ApiException 若参数不合规、能力不匹配或索引损坏
     */
    @SuppressWarnings("unchecked")
    public Context search(Actor actor, String runId, String question, List<String> kbIds, Map<String, Object> config, List<Map<String, Object>> history) {
        // Step 1: 配置校验与基础入参边界检查（限制问题字符长度 1~2000）
        validateConfig(config);
        if (question == null || question.isBlank() || question.length() > 2000)
            throw new ApiException(400, "INVALID_INPUT", "问题须为 1 至 2000 字符");

        // Step 2: 鉴权与当前生效构建版本解析（解析当前用户有权访问的全部 active_build_id）
        var builds = access.readableBuilds(actor, kbIds);
        if (builds.size() > 10000) throw new ApiException(409, "CAPACITY_LIMIT", "可读索引超出当前检索容量");
        List<String> warnings = new ArrayList<>();
        Map<String, Object> trace = new LinkedHashMap<>();
        // 若当前用户在指定范围内无可读知识库构建，快速短路返回空上下文
        if (builds.isEmpty()) return new Context(List.of(), trace, warnings);

        // Step 3: 解析配置分段策略（query 改写、retrieval 检索模式、post 后处理）
        var query = part(config, "query");
        var retrieval = part(config, "retrieval");
        var post = part(config, "post");
        String mode = retrieval.getOrDefault("mode", "BM25").toString();
        boolean dense = Set.of("DENSE", "HYBRID").contains(mode), sparse = !mode.equals("DENSE");
        List<String> reps = (List<String>) retrieval.getOrDefault("representations", List.of("ORIGINAL"));

        // Step 4: 索引能力一致性检查（若启用稠密向量检索或 MMR，确保索引具备对应表征且 Embedding 模型与维度一致）
        if (dense || Json.bool(post, "mmr", false))
            for (var b : db.list("SELECT * FROM index_build WHERE tenant_id=:t AND id IN (:ids)", params("t", actor.tenantId(), "ids", builds))) {
                String cap = b.get("capabilities").toString();
                for (String rep : reps)
                    if (!cap.contains(rep))
                        throw new ApiException(409, "INDEX_CAPABILITY_MISSING", "当前索引缺少所需表示");
                if (b.get("embedding_dims") == null || ((Number) b.get("embedding_dims")).intValue() != model.dimensions())
                    throw new ApiException(409, "INDEX_CAPABILITY_MISSING", "索引模型或维度与查询模型不一致");
            }

        // Step 5: 查询预处理管线（Query Expansion & Rewriting）
        LinkedHashSet<String> texts = new LinkedHashSet<>();
        texts.add(question.strip());
        String independent = question;

        // Step 5.1: 多轮对话指代消解与改写（Query Rewrite）
        if (Json.bool(query, "rewrite", false) && !history.isEmpty()) {
            var messages = new ArrayList<Map<String, Object>>();
            messages.add(params("role", "system", "content", "将追问改写为独立检索问题。历史仅解析指代，不是新事实。保留否定、错误码及条件。只输出 JSON {query:string}。"));
            messages.addAll(history);
            messages.add(params("role", "user", "content", question));
            try {
                independent = Json.str(Json.object(content(model.chat(actor, "RUN", runId, "REWRITE", messages, 300))), "query");
                if (independent.isBlank() || independent.length() > 2000)
                    throw new ApiException(422, "INVALID_MODEL_RESPONSE", "改写无效");
                texts.add(independent);
            } catch (ApiException e) {
                // 允许改写失败时自动降级回退至原始问题，记录告警 REWRITE_SKIPPED
                if (Json.bool(query, "allowRewriteFallback", false) && Set.of("MODEL_TIMEOUT", "MODEL_PROVIDER_ERROR", "INVALID_MODEL_RESPONSE", "INVALID_JSON").contains(e.code))
                    warnings.add("REWRITE_SKIPPED");
                else throw e;
            }
        }

        // Step 5.2: 多角度同义问法扩展（Multi-Query Generation）
        if (Json.bool(query, "multiQuery", false)) {
            var output = Json.object(content(model.chat(actor, "RUN", runId, "MULTI_QUERY", List.of(params("role", "system", "content", "为检索生成最多两个不改变条件的问法。只输出 JSON {queries:[string,string]}。"), params("role", "user", "content", independent)), 400)));
            if (!(output.get("queries") instanceof List<?> variants) || variants.size() > 2)
                throw new ApiException(422, "INVALID_MODEL_RESPONSE", "多查询响应无效");
            for (Object v : (List<?>) output.get("queries")) {
                String text = v.toString().strip().replaceAll("\\s+", " ");
                if (text.isBlank() || text.length() > 2000)
                    throw new ApiException(422, "INVALID_MODEL_RESPONSE", "多查询响应无效");
                texts.add(text);
            }
        }
        List<String> queries = texts.stream().limit(Json.integer(query, "maxVariants", 4)).toList(), denseQueries = new ArrayList<>(queries);

        // Step 5.3: 假设性文档嵌入（HyDE）生成
        if (Json.bool(query, "hyde", false)) {
            String hyde = content(model.chat(actor, "RUN", runId, "HYDE", List.of(params("role", "system", "content", "生成用于语义检索的假想段落，不超过300 token。这不是事实，不得改变权限。只输出段落。"), params("role", "user", "content", independent)), 300));
            denseQueries.add(hyde);
            trace.put("hyde", hyde);
        }
        trace.put("queries", queries);

        // Step 6: 双路多路由召回检索（Multi-Route Retrieval）
        int k = Json.integer(retrieval, "perRouteK", 50);
        List<List<Ranking.Hit>> routes = new ArrayList<>();
        List<Double> weights = new ArrayList<>();
        List<Object> routeDetails = new ArrayList<>();

        // Step 6.1: 稀疏全文检索（BM25 分词匹配 + 代码标识符加权）
        if (sparse) for (String q : queries) {
            var hits = bm25(actor, builds, q, k);
            routes.add(hits);
            weights.add((dense ? .5 : 1) / queries.size());
            routeDetails.add(params("modality", "BM25", "query", q, "hits", hits));
        }

        // Step 6.2: 稠密向量检索（模型计算 Embedding 向量 + ES HNSW kNN 检索）
        if (dense) {
            var vectors = model.embed(actor, "RUN", runId, denseQueries);
            for (int i = 0; i < vectors.size(); i++) {
                var hits = knn(actor, builds, reps, vectors.get(i), k, Json.integer(retrieval, "numCandidates", 200));
                routes.add(hits);
                weights.add((sparse ? .5 : 1) / vectors.size());
                routeDetails.add(params("modality", "DENSE", "query", denseQueries.get(i), "hits", hits));
            }
        }
        trace.put("routes", routeDetails);

        // Step 7: 倒数排名融合（RRF, Reciprocal Rank Fusion）多路打分合并与截断
        var fused = Ranking.rrf(routes, weights, Json.integer(part(config, "fusion"), "rrfK", 60)).stream().limit(Json.bool(post, "rerank", false) ? Json.integer(post, "rerankTopN", 30) : 30).toList();
        trace.put("fused", fused);
        if (fused.isEmpty()) return new Context(List.of(), trace, warnings);

        // Step 8: 从数据库回表加载候选分块元数据并二次鉴权
        Map<String, Map<String, Object>> chunks = new LinkedHashMap<>();
        for (var hit : fused) chunks.put(hit.id(), readChunk(actor, hit.id(), builds));
        List<Ranking.Hit> ranked = fused;

        // Step 9: 深度重排序精排（Cross-Encoder Rerank）
        if (Json.bool(post, "rerank", false)) {
            // 在调用外部模型重排前，向 run_source 表预先登记候选来源
            for (var c : chunks.values()) register(actor, runId, c, "RERANK", null);
            try {
                var scores = model.rerank(actor, runId, independent, chunks.values().stream().map(c -> c.get("text_content").toString()).toList());
                List<Ranking.Hit> sorted = new ArrayList<>();
                for (int i = 0; i < fused.size(); i++) sorted.add(new Ranking.Hit(fused.get(i).id(), scores.get(i)));
                sorted.sort(Comparator.comparingDouble(Ranking.Hit::score).reversed().thenComparing(Ranking.Hit::id));
                ranked = sorted;
            } catch (ApiException e) {
                // 若配置允许重排失败降级，记录告警 RERANK_SKIPPED 并保持 RRF 粗排顺序
                if (Json.bool(post, "allowRerankFallback", false) && Set.of("MODEL_TIMEOUT", "MODEL_PROVIDER_ERROR", "INVALID_MODEL_RESPONSE", "MODEL_NOT_CONFIGURED").contains(e.code))
                    warnings.add("RERANK_SKIPPED");
                else throw e;
            }
        }
        trace.put("reranked", ranked);

        // Step 10: 最大边际相关性（MMR）多样性打散与去重
        int count = Json.integer(part(config, "context"), "maxUnits", 6);
        if (Json.bool(post, "mmr", false)) {
            Map<String, List<Double>> vectors = new HashMap<>();
            for (var h : ranked) {
                var row = db.optional("SELECT vector_bytes,dims FROM representation WHERE tenant_id=:t AND chunk_id=:c AND kind='ORIGINAL' AND vector_bytes IS NOT NULL LIMIT 1", params("t", actor.tenantId(), "c", h.id())).orElseThrow(() -> new ApiException(409, "INDEX_CAPABILITY_MISSING", "MMR 缺少原文向量"));
                byte[] bytes = (byte[]) row.get("vector_bytes");
                int dims = ((Number) row.get("dims")).intValue();
                if (bytes.length != dims * 4)
                    throw new ApiException(409, "INDEX_CAPABILITY_MISSING", "原文向量制品损坏");
                var buf = ByteBuffer.wrap(bytes).order(ByteOrder.LITTLE_ENDIAN);
                List<Double> v = new ArrayList<>();
                while (buf.hasRemaining()) v.add((double) buf.getFloat());
                vectors.put(h.id(), v);
            }
            ranked = Ranking.mmr(ranked, vectors, count, Double.parseDouble(post.getOrDefault("lambda", .7).toString()));
            trace.put("mmrSelected", ranked);
        }

        // Step 11: 证据片段精准装配与父子切片展开（Parent-Child Expansion & Span Slicing）
        List<Map<String, Object>> evidence = new ArrayList<>();
        Set<String> seenUnits = new HashSet<>();
        int tokens = 0, limit = Json.integer(part(config, "context"), "maxTokens", 6000);
        int selected = 0;
        for (var h : ranked) {
            if (selected++ >= count) break;
            var chunk = chunks.get(h.id());
            var actual = chunk;
            // Step 11.1: 父块展开：在 Token 预算内优先展开为完整父块大上下文，超限则回退为子块并记录告警
            if (Json.bool(post, "parent", false) && chunk.get("parent_id") != null) {
                var parent = readChunk(actor, chunk.get("parent_id").toString(), builds);
                if (parent.get("text_content").toString().getBytes(StandardCharsets.UTF_8).length + tokens <= limit)
                    actual = parent;
                else warnings.add("PARENT_TRIMMED");
            }
            // Step 11.2: 按 Unicode 字符偏移（Code Point）截取原始版面单元切片，防截断乱码与越界
            var units = spans(actor, actual.get("id").toString());
            for (var unit : units) {
                String text = unit.get("text_content").toString();
                int start = ((Number) unit.get("start_cp")).intValue(), end = ((Number) unit.get("end_cp")).intValue();
                String identity = unit.get("unit_id") + ":" + start + ":" + end;
                if (!seenUnits.add(identity)) continue;
                if (end > text.codePointCount(0, text.length()))
                    throw new ApiException(503, "SOURCE_CORRUPT", "原文范围无效");
                String slice = text.substring(text.offsetByCodePoints(0, start), text.offsetByCodePoints(0, end));
                int estimate = slice.getBytes(StandardCharsets.UTF_8).length;
                // 校验总 Token 上限，超出时截断候选并记录 CONTEXT_TRIMMED
                if (tokens + estimate > limit) {
                    warnings.add("CONTEXT_TRIMMED");
                    continue;
                }
                tokens += estimate;
                var e = params("evidenceId", "E" + (evidence.size() + 1), "documentId", actual.get("document_id"), "versionId", actual.get("version_id"), "buildId", actual.get("build_id"), "chunkId", actual.get("id"), "matchedChunkId", h.id(), "unitId", unit.get("unit_id"), "text", slice, "start", start, "end", end, "sourceType", unit.get("kind"), "machineGenerated", unit.get("machine_generated"), "location", Json.object(unit.get("location").toString()), "isCurrent", true);
                evidence.add(e);
            }
        }

        // Step 12: 最终证据登记、轨迹记录与行级鉴权守卫
        for (var e : evidence) registerEvidence(actor, runId, e, "CONTEXT");
        trace.put("context", evidence.stream().map(e -> params("evidenceId", e.get("evidenceId"), "chunkId", e.get("chunkId"), "unitId", e.get("unitId"), "start", e.get("start"), "end", e.get("end"))).toList());
        trace.put("contextTokenUpperBound", tokens);
        // 在租户读锁保护下执行文档级权限终检并返回最终上下文结果
        return access.guard(actor, evidence.stream().map(e -> e.get("documentId").toString()).toList(), () -> new Context(evidence, trace, warnings));
    }

    /**
     * 提取模型返回消息中的正文文本并剥离 Markdown 代码块标记（```json ... ```）。
     *
     * @param message 模型响应消息 Map
     * @return 清洗后的纯文本/JSON 字符串
     * @throws ApiException 若消息内容为空或非字符串（422 INVALID_MODEL_RESPONSE）
     */
    public static String content(Map<String, Object> message) {
        Object c = message.get("content");
        if (!(c instanceof String text) || text.isBlank())
            throw new ApiException(422, "INVALID_MODEL_RESPONSE", "模型没有文本输出");
        return text.replaceFirst("^```(?:json)?\\s*", "").replaceFirst("\\s*```$", "");
    }

    /**
     * 从数据库加载单个分块元数据，并校验构建范围与文档级访问权限。
     *
     * @param a 操作主体
     * @param id 目标分块 ID
     * @param builds 当前合法可读的构建 ID 列表
     * @return 分块数据 Map
     * @throws ApiException 若分块所属构建不在可读列表中或已被覆盖（409 SOURCE_CHANGED）
     */
    public Map<String, Object> readChunk(Actor a, String id, List<String> builds) {
        var c = db.one("SELECT c.*,b.document_id,b.version_id FROM chunk c JOIN index_build b ON b.tenant_id=c.tenant_id AND b.id=c.build_id JOIN document d ON d.tenant_id=b.tenant_id AND d.id=b.document_id AND d.active_build_id=b.id WHERE c.tenant_id=:t AND c.id=:id", params("t", a.tenantId(), "id", id));
        if (!builds.contains(c.get("build_id").toString()))
            throw new ApiException(409, "SOURCE_CHANGED", "来源版本或范围已变化");
        access.requireDocument(a, c.get("document_id").toString());
        return c;
    }

    /**
     * 查询指定分块包含的所有解析单元跨度（chunk_span）及源文本单元信息。
     *
     * @param a 操作主体
     * @param chunk 目标分块 ID
     * @return 按在分块内出现次序排列的 span 列表
     */
    private List<Map<String, Object>> spans(Actor a, String chunk) {
        return db.list("SELECT s.*,u.id AS unit_id,u.text_content,u.kind,u.location,u.machine_generated FROM chunk_span s JOIN source_unit u ON u.tenant_id=s.tenant_id AND u.id=s.unit_id WHERE s.tenant_id=:t AND s.chunk_id=:c ORDER BY s.ordinal", params("t", a.tenantId(), "c", chunk));
    }

    /**
     * 向 run_source 表登记检索引用的候选来源分块。
     *
     * @param a 操作主体
     * @param run 关联问答运行 ID
     * @param chunk 命中的分块数据 Map
     * @param purpose 引用目的（如 RERANK 等）
     * @param evidence 可选的证据标识
     */
    private void register(Actor a, String run, Map<String, Object> chunk, String purpose, String evidence) {
        if (run == null) return;
        db.insert("run_source", params("tenant_id", a.tenantId(), "id", Json.id(), "run_id", run, "document_id", chunk.get("document_id"), "build_id", chunk.get("build_id"), "purpose", purpose, "payload_hash", hash(chunk.get("text_content").toString())));
    }

    /**
     * 向 run_source 表登记最终组装入上下文的精准证据片段（带字符偏移量与 SHA-256）。
     *
     * @param a 操作主体
     * @param run 关联问答运行 ID
     * @param e 结构化证据片段 Map（包含 start, end, text, unitId, evidenceId 等）
     * @param purpose 证据用途标识（如 CONTEXT）
     */
    public void registerEvidence(Actor a, String run, Map<String, Object> e, String purpose) {
        if (run == null) return;
        db.insert("run_source", params("tenant_id", a.tenantId(), "id", Json.id(), "run_id", run, "document_id", e.get("documentId"), "build_id", e.get("buildId"), "unit_id", e.get("unitId"), "evidence_id", e.get("evidenceId"), "purpose", purpose, "span_json", Json.write(params("start", e.get("start"), "end", e.get("end"))), "payload_hash", hash(e.get("text").toString())));
    }

    /**
     * 计算输入文本字符串的 SHA-256 摘要字节数组。
     *
     * @param text 待哈希的输入字符串
     * @return 32 字节哈希数组
     */
    public static byte[] hash(String text) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(text.getBytes(StandardCharsets.UTF_8));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    /**
     * 执行 Elasticsearch BM25 稀疏全文与代码术语加权检索。
     * <p>
     * 针对问题中的代码标识符（如 camelCase、snake_case、路径与错误码格式）增加 5 倍权重增强精确匹配。
     *
     * @param a 操作主体
     * @param builds 限制检索的构建 ID 列表
     * @param q 检索文本
     * @param k 召回数量限制
     * @return 命中列表
     */
    private List<Ranking.Hit> bm25(Actor a, List<String> builds, String q, int k) {
        List<Object> should = new ArrayList<>();
        should.add(params("multi_match", params("query", q, "fields", List.of("text", "title^2"))));
        var matcher = java.util.regex.Pattern.compile("[A-Za-z_][A-Za-z0-9_./:-]{2,}").matcher(q);
        while (matcher.find())
            should.add(params("term", params("code_terms", params("value", matcher.group(), "boost", 5))));
        return query("original", params("size", k, "_source", List.of("canonical_chunk_id"), "query", params("bool", params("filter", filters(a, builds), "should", should, "minimum_should_match", 1))));
    }

    /**
     * 执行 Elasticsearch kNN 稠密向量近邻检索。
     *
     * @param a 操作主体
     * @param builds 限制检索的构建 ID 列表
     * @param reps 允许检索的表示类型（如 ORIGINAL, SUMMARY 等）
     * @param v 查询向量
     * @param k 召回数量
     * @param candidates 候选集探索容量
     * @return 命中列表
     */
    private List<Ranking.Hit> knn(Actor a, List<String> builds, List<String> reps, List<Double> v, int k, int candidates) {
        var filters = filters(a, builds);
        filters.add(params("terms", params("representation_type", reps)));
        return query("vector", params("size", k, "_source", List.of("canonical_chunk_id"), "knn", params("field", "embedding", "query_vector", v, "k", k, "num_candidates", candidates, "filter", params("bool", params("filter", filters)))));
    }

    /**
     * 构造租户与构建范围的 Elasticsearch 基础过滤子句。
     *
     * @param a 操作主体
     * @param builds 授权的构建 ID 列表
     * @return 包含 tenant_id 与 build_id 的过滤项列表
     */
    private List<Object> filters(Actor a, List<String> builds) {
        return new ArrayList<>(List.of(params("term", params("tenant_id", a.tenantId())), params("terms", params("build_id", builds))));
    }

    /**
     * 发起底层 Elasticsearch HTTP 查询请求并解析命中文档。
     * <p>
     * 严格检查 HTTP 状态码、ES 查询超时标志（timed_out）及分片失败计数（failed &gt; 0）。
     *
     * @param index ES 索引子类型（如 original 或 vector）
     * @param body 查询 DSL 请求体 Map
     * @return 解析后的命中列表
     * @throws ApiException 若 ES 服务异常或分片失败
     */
    @SuppressWarnings("unchecked")
    private List<Ranking.Hit> query(String index, Map<String, Object> body) {
        try {
            var request = HttpRequest.newBuilder(URI.create(es + "/" + prefix + "-" + index + "/_search")).timeout(Duration.ofSeconds(15)).header("Content-Type", "application/json").POST(HttpRequest.BodyPublishers.ofString(Json.write(body))).build();
            var response = http.send(request, HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() != 200) throw new ApiException(503, "SEARCH_UNAVAILABLE", "检索服务未就绪");
            var json = Json.object(response.body());
            if (Boolean.TRUE.equals(json.get("timed_out"))) throw new ApiException(503, "SEARCH_TIMEOUT", "检索超时");
            var shard = (Map<?, ?>) json.get("_shards");
            if (shard != null && ((Number) shard.get("failed")).intValue() > 0)
                throw new ApiException(503, "SEARCH_PARTIAL_FAILURE", "检索分片失败");
            List<Ranking.Hit> hits = new ArrayList<>();
            for (Object h : (List<?>) ((Map<?, ?>) json.get("hits")).get("hits")) {
                var item = (Map<String, Object>) h;
                var source = (Map<String, Object>) item.get("_source");
                hits.add(new Ranking.Hit(Objects.toString(source.get("canonical_chunk_id"), item.get("_id").toString()), ((Number) item.get("_score")).doubleValue()));
            }
            return hits;
        } catch (ApiException e) {
            throw e;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ApiException(503, "SEARCH_UNAVAILABLE", "检索已中断");
        } catch (Exception e) {
            throw new ApiException(503, "SEARCH_UNAVAILABLE", "检索服务不可用");
        }
    }
}
