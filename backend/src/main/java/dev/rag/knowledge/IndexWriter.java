package dev.rag.knowledge;

import dev.rag.common.*;
import org.springframework.stereotype.Component;
import org.springframework.beans.factory.annotation.Value;

import java.net.*;
import java.net.http.*;
import java.time.Duration;
import java.util.*;
import java.nio.*;

/**
 * 搜索引擎与向量数据库索引写入核心组件（Elasticsearch / OpenSearch 适配）。
 * 负责管理全文索引与密集向量索引（HNSW 索引）、双路索引写入、刷新（Refresh）、全量校验（Verify）及清理（Delete By Query）。
 */
@Component
public class IndexWriter {
    private final Db db;
    private final String url, prefix;
    private final HttpClient client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

    /**
     * 构造 IndexWriter 实例并校验索引前缀安全性。
     *
     * @param db     数据库访问组件
     * @param url    Elasticsearch/OpenSearch 服务的根 URL
     * @param prefix 索引命名空间前缀（限制为 [a-z0-9_-]+）
     * @throws IllegalArgumentException 若索引前缀字符集不合法
     */
    public IndexWriter(Db db, @Value("${rag.elasticsearch-url}") String url, @Value("${rag.index-prefix}") String prefix) {
        this.db = db;
        this.url = url;
        this.prefix = prefix;
        if (!prefix.matches("[a-z0-9_-]+")) throw new IllegalArgumentException("Invalid index prefix");
    }

    /**
     * 发送 Elasticsearch REST API 请求并解析 JSON 响应。
     *
     * @param method HTTP 请求方法 (GET, POST, PUT, HEAD)
     * @param path   API 相对路径（如 /_refresh 或 /_doc/id）
     * @param body   JSON 请求体文本（可为 null）
     * @return 响应体解析后的 Map；针对 HEAD 请求返回包含 {"exists": boolean}
     * @throws ApiException 若状态码异常或网络不可达（503 INDEX_UNAVAILABLE）
     */
    @SuppressWarnings("unchecked")
    public Map<String, Object> request(String method, String path, String body) {
        try {
            var b = HttpRequest.newBuilder(URI.create(url + path)).timeout(Duration.ofSeconds(30)).header("Content-Type", path.contains("_bulk") ? "application/x-ndjson" : "application/json").method(method, body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofString(body));
            var response = client.send(b.build(), HttpResponse.BodyHandlers.ofString());
            if (response.statusCode() == 404 && method.equals("HEAD")) return Map.of("exists", false);
            if (response.statusCode() < 200 || response.statusCode() >= 300)
                throw new ApiException(503, "INDEX_UNAVAILABLE", "索引服务不可用: " + response.statusCode() + " " + response.body());
            return response.body().isBlank() ? Map.of("exists", true) : Json.object(response.body());
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            throw new ApiException(503, "INDEX_UNAVAILABLE", "索引调用中断");
        } catch (java.io.IOException e) {
            throw new ApiException(503, "INDEX_UNAVAILABLE", "索引服务不可用: " + e.getMessage());
        }
    }

    /**
     * 确保存储索引结构与 Mapping 存在。
     * 若索引不存在，则按照规范严格初始化：
     * - 全文索引 (-original)：配置 CJK 中日韩双字分词器，字段动态映射设为 strict 严格模式；
     * - 向量索引 (-vector)：配置 dense_vector，指定维度 dims，启用 HNSW 索引图与余弦相似度 (cosine)。
     *
     * @param vector 是否为向量索引
     * @param dims   向量维度（仅向量索引有效）
     */
    private void ensure(boolean vector, int dims) {
        String index = prefix + (vector ? "-vector" : "-original");
        if (Boolean.FALSE.equals(request("HEAD", "/" + index, null).get("exists"))) {
            Map<String, Object> fields = new LinkedHashMap<>();
            for (String k : vector ? List.of("tenant_id", "document_id", "version_id", "build_id", "canonical_chunk_id", "representation_id", "representation_type", "model_fingerprint", "text_hash") : List.of("tenant_id", "kb_id", "document_id", "version_id", "build_id", "canonical_chunk_id", "code_terms", "content_hash"))
                fields.put(k, Map.of("type", "keyword"));
            if (vector)
                fields.put("embedding", Map.of("type", "dense_vector", "dims", dims, "index", true, "similarity", "cosine", "index_options", Map.of("type", "hnsw")));
            else {
                fields.put("title", Map.of("type", "text", "analyzer", "cjk"));
                fields.put("text", Map.of("type", "text", "analyzer", "cjk"));
            }
            request("PUT", "/" + index, Json.write(Map.of("settings", Map.of("number_of_shards", 1, "number_of_replicas", 0), "mappings", Map.of("dynamic", "strict", "properties", fields))));
        }
    }

    /**
     * 将指定构建版本的所有子分块与向量表征写入搜索引擎。
     * <p>
     * <b>重要执行流程：</b>
     * 1. 查询 index_build 与 document 元信息，确保全文索引 schema 存在；
     * 2. 提取所有 CHILD 文本分块，逐条写入 -original 全文索引；
     * 3. 提取所有计算完毕的稠密向量，小端序解码 Float 向量数组后写入 -vector 索引；
     * 4. 强制执行索引刷新（_refresh），使写入数据立即对搜索可见；
     * 5. 调用 verify 逐一断言全文与向量记录条数与字段完整性，确保索引绝对可靠。
     *
     * @param tenant 租户 ID
     * @param build  构建版本 ID
     * @throws ApiException 若索引写入失败、格式错误或校验不通过
     */
    public void write(String tenant, String build) {
        var b = db.one("SELECT b.*,d.kb_id,d.title FROM index_build b JOIN document d ON d.tenant_id=b.tenant_id AND d.id=b.document_id WHERE b.tenant_id=:t AND b.id=:b", Db.params("t", tenant, "b", build));
        ensure(false, 0);
        var chunks = db.list("SELECT * FROM chunk WHERE tenant_id=:t AND build_id=:b AND kind='CHILD'", Db.params("t", tenant, "b", build));
        for (var c : chunks) {
            Map<String, Object> source = Db.params("tenant_id", tenant, "kb_id", b.get("kb_id"), "document_id", b.get("document_id"), "version_id", b.get("version_id"), "build_id", build, "canonical_chunk_id", c.get("id"), "title", b.get("title"), "text", c.get("text_content"), "content_hash", HexFormat.of().formatHex((byte[]) c.get("content_hash")));
            put(prefix + "-original", c.get("id").toString(), source);
        }
        var reps = db.list("SELECT * FROM representation WHERE tenant_id=:t AND build_id=:b AND vector_bytes IS NOT NULL", Db.params("t", tenant, "b", build));
        if (!reps.isEmpty()) ensure(true, ((Number) reps.getFirst().get("dims")).intValue());
        for (var r : reps) {
            ByteBuffer buffer = ByteBuffer.wrap((byte[]) r.get("vector_bytes")).order(ByteOrder.LITTLE_ENDIAN);
            List<Float> vector = new ArrayList<>();
            while (buffer.hasRemaining()) vector.add(buffer.getFloat());
            put(prefix + "-vector", r.get("id").toString(), Db.params("tenant_id", tenant, "document_id", b.get("document_id"), "version_id", b.get("version_id"), "build_id", build, "canonical_chunk_id", r.get("chunk_id"), "representation_id", r.get("id"), "representation_type", r.get("kind"), "model_fingerprint", r.get("model_fingerprint"), "text_hash", HexFormat.of().formatHex((byte[]) r.get("text_hash")), "embedding", vector));
        }
        request("POST", "/" + prefix + "-original/_refresh", null);
        if (!reps.isEmpty()) request("POST", "/" + prefix + "-vector/_refresh", null);
        verify(tenant, build, chunks.stream().map(c -> c.get("id").toString()).toList(), reps.stream().map(r -> r.get("id").toString()).toList());
    }

    /**
     * 单条文档写入。
     *
     * @param index  目标索引名称
     * @param id     文档唯一主键
     * @param source 写入字段 Map
     */
    private void put(String index, String id, Map<String, Object> source) {
        request("PUT", "/" + index + "/_doc/" + id, Json.write(source));
    }

    /**
     * 校验本次构建在搜索引擎中的全文与向量索引完整度。
     *
     * @param t         租户 ID
     * @param b         构建版本 ID
     * @param originals 预期的全部全文分块 ID 列表
     * @param vectors   预期的全部向量表征 ID 列表
     */
    public void verify(String t, String b, List<String> originals, List<String> vectors) {
        verifyIds(prefix + "-original", t, b, originals);
        if (!vectors.isEmpty()) verifyIds(prefix + "-vector", t, b, vectors);
    }

    /**
     * 批量比对索引条目数量与字段一致性。
     * 使用 _count 确认总条数，使用 _mget（分批 100 条）核对每个分块的存在性与租户/构建 ID 匹配。
     *
     * @param index    目标索引名称
     * @param t        租户 ID
     * @param build    构建版本 ID
     * @param expected 预期存在的 ID 列表
     * @throws ApiException 若条数不一致或存在缺失/元数据不符（503 INDEX_INCOMPLETE）
     */
    @SuppressWarnings("unchecked")
    private void verifyIds(String index, String t, String build, List<String> expected) {
        if (expected.isEmpty()) throw new ApiException(422, "EMPTY_INDEX", "索引无有效块");
        Object query = Map.of("bool", Map.of("filter", List.of(Map.of("term", Map.of("tenant_id", t)), Map.of("term", Map.of("build_id", build)))));
        var count = request("POST", "/" + index + "/_count", Json.write(Map.of("query", query)));
        if (((Number) count.get("count")).intValue() != expected.size())
            throw new ApiException(503, "INDEX_INCOMPLETE", "索引条目数量不匹配");
        for (int i = 0; i < expected.size(); i += 100) {
            List<String> ids = expected.subList(i, Math.min(i + 100, expected.size()));
            var m = request("POST", "/" + index + "/_mget", Json.write(Map.of("ids", ids)));
            for (var row : (List<Map<String, Object>>) m.get("docs")) {
                var source = (Map<String, Object>) row.get("_source");
                if (!Boolean.TRUE.equals(row.get("found")) || source == null || !t.equals(source.get("tenant_id")) || !build.equals(source.get("build_id")))
                    throw new ApiException(503, "INDEX_INCOMPLETE", "索引条目不完整");
            }
        }
    }

    /**
     * 按租户和文档 ID 物理清除 Elasticsearch 中的全部全文和向量索引记录。
     * 采用 _delete_by_query 并开启 refresh 立即生效。
     *
     * @param tenant 租户 ID
     * @param doc    文档 ID
     * @throws ApiException 若索引清理发生超时或内部失败
     */
    public void delete(String tenant, String doc) {
        for (String suffix : List.of("-original", "-vector")) {
            String index = prefix + suffix;
            if (Boolean.FALSE.equals(request("HEAD", "/" + index, null).get("exists"))) continue;
            var r = request("POST", "/" + index + "/_delete_by_query?refresh=true&conflicts=proceed", Json.write(Map.of("query", Map.of("bool", Map.of("filter", List.of(Map.of("term", Map.of("tenant_id", tenant)), Map.of("term", Map.of("document_id", doc))))))));
            if (Boolean.TRUE.equals(r.get("timed_out")) || ((List<?>) r.getOrDefault("failures", List.of())).size() > 0)
                throw new ApiException(503, "INDEX_DELETE_INCOMPLETE", "索引清理未完成");
        }
    }
}
