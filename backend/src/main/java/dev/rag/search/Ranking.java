package dev.rag.search;

import java.util.*;

/**
 * 检索结果排序与多路融合核心算法工具类。
 * <p>
 * 提供多路混合检索中标准的加权倒数排名融合（Weighted Reciprocal Rank Fusion, RRF）、
 * 向量余弦相似度（Cosine Similarity）以及兼顾多样性与去重的最大边际相关性（Maximal Marginal Relevance, MMR）算法。
 */
public final class Ranking {
    private Ranking() {
    }

    /**
     * 命中结果记录项。
     *
     * @param id 命中的文档/块唯一标识 ID
     * @param score 相关性得分或融合排名权重分
     */
    public record Hit(String id, double score) {
    }

    /**
     * 加权倒数排名融合（Weighted Reciprocal Rank Fusion, RRF）。
     * <p>
     * 将多个异构检索通路（如 BM25 全文检索、稠密向量检索等）的有序候选列表融合为统一评分列表。
     * 消除不同模型检索分数的量纲差异，仅依赖排序位次进行无偏融合。
     * <p>
     * <b>数学计算公式：</b>
     * <pre>
     * RRF_Score(d) = &sum; [ weight_i / (k + rank_i(d)) ]
     * </pre>
     * 其中：
     * <ul>
     *   <li>{@code i}：检索通路索引；</li>
     *   <li>{@code weight_i}：该检索通路的加权系数；</li>
     *   <li>{@code k}：平滑常数（通常取 60，防止排名前列的文档权重占比过大）；</li>
     *   <li>{@code rank_i(d)}：文档 {@code d} 在通路 {@code i} 中的 1-indexed 唯一名次（自动去重）。</li>
     * </ul>
     *
     * @param routes 多路检索的命中列表集合（每个列表内部已预排序）
     * @param weights 与各检索通路一一对应的浮点权重系数列表
     * @param k RRF 平滑常数（必须 &gt;= 1）
     * @return 按融合总分降序排列的统一命中列表（得分相同按 ID 字典序升序）
     * @throws IllegalArgumentException 若通道数与权重数不匹配或 k 小于 1
     */
    public static List<Hit> rrf(List<List<Hit>> routes, List<Double> weights, int k) {
        if (routes.size() != weights.size() || k < 1) throw new IllegalArgumentException("Invalid RRF configuration");
        Map<String, Double> scores = new HashMap<>();
        for (int i = 0; i < routes.size(); i++) {
            Set<String> seen = new HashSet<>();
            int rank = 0;
            for (Hit h : routes.get(i))
                if (seen.add(h.id())) scores.merge(h.id(), weights.get(i) / (k + (double) ++rank), Double::sum);
        }
        return scores.entrySet().stream().map(e -> new Hit(e.getKey(), e.getValue())).sorted(Comparator.comparingDouble(Hit::score).reversed().thenComparing(Hit::id)).toList();
    }

    /**
     * 计算两个高维浮点向量的余弦相似度（Cosine Similarity）。
     * <p>
     * <b>数学公式：</b>
     * <pre>
     * Cosine(a, b) = (a &middot; b) / (||a||_2 &times; ||b||_2)
     * </pre>
     *
     * @param a 第一个向量
     * @param b 第二个向量（维度必须与 a 相同）
     * @return 介于 [-1.0, 1.0] 区间内的余弦相似度标量
     * @throws IllegalArgumentException 若两向量维度不一致、为空或任一向量模长为 0
     */
    public static double cosine(List<Double> a, List<Double> b) {
        if (a.size() != b.size() || a.isEmpty()) throw new IllegalArgumentException("Incompatible vectors");
        double dot = 0, x = 0, y = 0;
        for (int i = 0; i < a.size(); i++) {
            dot += a.get(i) * b.get(i);
            x += a.get(i) * a.get(i);
            y += b.get(i) * b.get(i);
        }
        if (x == 0 || y == 0) throw new IllegalArgumentException("Zero vector");
        return Math.max(-1, Math.min(1, dot / Math.sqrt(x * y)));
    }

    /**
     * 最大边际相关性（Maximal Marginal Relevance, MMR）重排序算法。
     * <p>
     * 旨在从初筛候选集合中贪心挑选既与查询高相关、彼此之间又具有多样性（低信息冗余）的最终子集。
     * <p>
     * <b>贪心选择目标：</b>
     * <pre>
     * MMR = argmax [ &lambda; * Sim1(d_i, Query) - (1 - &lambda;) * max_{d_j &isin; Selected} Sim2(d_i, d_j) ]
     * </pre>
     * <b>工程实现细节：</b>
     * <ul>
     *   <li>相关性得分归一化：将初筛原始分数通过 Min-Max 缩放至 [0, 1] 区间（若全相同则固定为 1）；</li>
     *   <li>向量冗余度度量：计算未入选向量与已选向量之间的余弦相似度，并映射至 [0, 1]：{@code (cosine + 1) / 2}；</li>
     *   <li>参数 &lambda; 权衡：&lambda;=1 退化为纯相关性排序，&lambda;=0 退化为纯多样性排斥。</li>
     * </ul>
     *
     * @param input 候选命中结果列表
     * @param vectors 各命中 ID 对应的稠密特征向量 Map
     * @param count 目标重排选择保留的最终结果数量
     * @param lambda 边际收益权衡参数，取值范围 [0.0, 1.0]
     * @return 选出的 Top-N 多样化命中列表
     * @throws IllegalArgumentException 若 lambda 超出 [0, 1] 区间
     */
    public static List<Hit> mmr(List<Hit> input, Map<String, List<Double>> vectors, int count, double lambda) {
        if (lambda < 0 || lambda > 1) throw new IllegalArgumentException("Invalid MMR lambda");
        if (input.isEmpty()) return List.of();
        double min = input.stream().mapToDouble(Hit::score).min().orElseThrow(), max = input.stream().mapToDouble(Hit::score).max().orElseThrow();
        List<Hit> remaining = new ArrayList<>(input), selected = new ArrayList<>();
        while (!remaining.isEmpty() && selected.size() < count) {
            Hit best = null;
            double bestScore = -Double.MAX_VALUE;
            for (Hit h : remaining) {
                double relevance = max == min ? 1 : (h.score() - min) / (max - min), redundancy = 0;
                for (Hit s : selected)
                    redundancy = Math.max(redundancy, (cosine(Objects.requireNonNull(vectors.get(h.id())), Objects.requireNonNull(vectors.get(s.id()))) + 1) / 2);
                double value = lambda * relevance - (1 - lambda) * redundancy;
                if (value > bestScore) {
                    best = h;
                    bestScore = value;
                }
            }
            selected.add(best);
            remaining.remove(best);
        }
        return selected;
    }
}
