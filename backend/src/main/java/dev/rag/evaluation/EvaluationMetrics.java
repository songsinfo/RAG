package dev.rag.evaluation;

import java.util.*;

/**
 * 离线评测检索质量与排序性能核心指标算法工具类。
 * <p>
 * 提供业界标准的信息检索（IR）与大模型评测指标计算：
 * <ul>
 *   <li><b>Hit@K（命中率）</b>：Top-K 检索切片中是否至少命中了 1 个金标准事实组；</li>
 *   <li><b>Recall@K（召回率）</b>：Top-K 检索切片所覆盖的金标准事实组比例；</li>
 *   <li><b>MRR@K（平均倒数排名）</b>：首个相关切片所在排名位置的倒数 \( 1 / \text{rank} \)；</li>
 *   <li><b>NDCG@K（归一化折损累计增益）</b>：衡量多级相关度（0~3 级）排序质量的标准指标；</li>
 *   <li><b>Percentile（分位数）</b>：用于统计评测响应耗时分布（P50、P95、P99）。</li>
 * </ul>
 */
public final class EvaluationMetrics {
    /**
     * 工具类私有构造函数，禁止实例化。
     */
    private EvaluationMetrics() {
    }

    /**
     * 安全计算两数比例（分子 / 分母）。
     *
     * @param numerator 分子
     * @param denominator 分母
     * @return 比例浮点值；若分母为 0 则返回 {@code null} 避免除以零异常
     */
    public static Double ratio(long numerator, long denominator) {
        return denominator == 0 ? null : (double) numerator / denominator;
    }

    /**
     * 计算检索切片列表在 Top-K 截断下的多项核心二元相关性检索指标（Hit, Recall, MRR）。
     * <p>
     * <b>事实组（Fact Groups）评估机制：</b>
     * 评测金标准通常按“事实组”划分（例如一个复杂问题需要 2 个不同事实支撑），每个事实组内可能包含
     * 多个等价的候选切片 ID。检索切片只要命中了该事实组内任意一个候选切片，即判定该事实组被成功召回。
     * <p>
     * <b>指标计算公式：</b>
     * <ul>
     *   <li>\( \text{Hit@K} = \begin{cases} 1.0, & \text{若命中了至少一个事实组} \\ 0.0, & \text{否则} \end{cases} \)</li>
     *   <li>\( \text{Recall@K} = \frac{|\text{已召回的事实组数量}|}{|\text{总事实组数量}|} \)</li>
     *   <li>\( \text{MRR@K} = \frac{1}{\text{first\_relevant\_rank}} \)（若 Top-K 内无任何相关切片则为 0.0）</li>
     * </ul>
     *
     * @param groups 金标准事实组列表，每个事实组由一组等价的切片标识集合组成
     * @param ranked 检索系统按排序得分降序返回的切片标识集合列表
     * @param k 截断窗口大小（Top-K，必须大于等于 1）
     * @return 包含 "hit"、"recall"、"mrr" 键的指标计算结果 Map
     * @throws IllegalArgumentException 若 k 小于 1 或存在空的事实组
     */
    public static Map<String, Double> retrieval(List<Set<String>> groups, List<Set<String>> ranked, int k) {
        if (k < 1) throw new IllegalArgumentException("k must be positive");
        Map<String, Double> result = new LinkedHashMap<>();
        if (groups.isEmpty()) {
            result.put("hit", null);
            result.put("recall", null);
            result.put("mrr", null);
            return result;
        }
        if (groups.stream().anyMatch(Set::isEmpty)) throw new IllegalArgumentException("Empty fact group");
        Set<Integer> found = new HashSet<>();
        int first = 0;
        for (int rank = 0; rank < Math.min(k, ranked.size()); rank++) {
            boolean relevant = false;
            for (int g = 0; g < groups.size(); g++)
                if (!Collections.disjoint(groups.get(g), ranked.get(rank))) {
                    found.add(g);
                    relevant = true;
                }
            if (relevant && first == 0) first = rank + 1;
        }
        result.put("hit", found.isEmpty() ? 0.0 : 1.0);
        result.put("recall", (double) found.size() / groups.size());
        result.put("mrr", first == 0 ? 0.0 : 1.0 / first);
        return result;
    }

    /**
     * 综合计算检索系统的二元与多级相关性指标（包含 Hit@K、Recall@K、MRR@K 以及 NDCG@K）。
     *
     * @param groups 金标准事实组列表
     * @param ranked 检索返回的排序切片集合列表
     * @param rankedGrades 检索返回的实际文档相关度级别列表（0~3 级）
     * @param idealGrades 理想排序下的文档相关度级别列表（0~3 级）
     * @param k 截断窗口大小（Top-K）
     * @return 包含 "hit", "recall", "mrr", "ndcg" 的综合指标结果 Map
     */
    public static Map<String, Double> retrieval(List<Set<String>> groups, List<Set<String>> ranked, List<Integer> rankedGrades, List<Integer> idealGrades, int k) {
        var result = new LinkedHashMap<>(retrieval(groups, ranked, k));
        result.put("ndcg", ndcg(rankedGrades, idealGrades, k));
        return result;
    }

    /**
     * 计算 Top-K 归一化折损累计增益（Normalized Discounted Cumulative Gain, NDCG@K）。
     * <p>
     * <b>数学公式：</b>
     * \[
     * \text{NDCG}@K = \frac{\text{DCG}@K}{\text{IDCG}@K}
     * \]
     * 其中 \( \text{IDCG}@K \) 为将理想相关度等级（ideal）按降序排序后计算得到的最大可能 DCG@K。
     * 若 \( \text{IDCG}@K = 0 \)，则返回 {@code null}。
     *
     * @param ranked 实际检索排序的相关度等级列表（取值必须在 0..3 范围内）
     * @param ideal 理想或标注的所有相关度等级列表（取值必须在 0..3 范围内）
     * @param k 截断深度
     * @return NDCG@K 评价值（介于 0.0 与 1.0 之间）；若理想 DCG 为 0 则返回 {@code null}
     * @throws IllegalArgumentException 若 k 小于 1 或等级超出 0..3 范围
     */
    public static Double ndcg(List<Integer> ranked, List<Integer> ideal, int k) {
        if (k < 1) throw new IllegalArgumentException("k must be positive");
        if (ranked.stream().anyMatch(g -> g < 0 || g > 3) || ideal.stream().anyMatch(g -> g < 0 || g > 3))
            throw new IllegalArgumentException("Grade must be 0..3; unjudged is not zero");
        double denominator = dcg(ideal.stream().sorted(Comparator.reverseOrder()).toList(), k);
        return denominator == 0 ? null : dcg(ranked, k) / denominator;
    }

    /**
     * 计算折损累计增益（Discounted Cumulative Gain, DCG@K）。
     * <p>
     * 采用工业界常用的指数增益与以 2 为底的位置折损公式：
     * \[
     * \text{DCG}@K = \sum_{i=1}^{\min(K, |grades|)} \frac{2^{r_i} - 1}{\log_2(i + 1)}
     * \]
     * 其中 \( r_i \) 为第 \( i \) 个位置的文档相关度评级。
     *
     * @param grades 相关度评级列表
     * @param k 截断窗口大小
     * @return 计算得到的 DCG 累加值
     */
    private static double dcg(List<Integer> grades, int k) {
        double total = 0;
        for (int i = 0; i < Math.min(k, grades.size()); i++)
            total += (Math.pow(2, grades.get(i)) - 1) / (Math.log(i + 2) / Math.log(2));
        return total;
    }

    /**
     * 计算有序数值序列的指定百分位数（Percentile）。
     * <p>
     * 常用于统计批次评测运行的耗时（如 P50, P95, P99）。
     * 采用向上取整定位算法：\( \text{index} = \lceil N \times \text{quantile} \rceil - 1 \)。
     *
     * @param values 待统计数值列表
     * @param quantile 分位点（取值范围：(0, 1]）
     * @return 处于该分位点上的数值；列表为空时返回 {@code null}
     * @throws IllegalArgumentException 若 quantile 超出 (0, 1] 范围
     */
    public static Long percentile(List<Long> values, double quantile) {
        if (quantile <= 0 || quantile > 1) throw new IllegalArgumentException("quantile must be (0,1]");
        if (values.isEmpty()) return null;
        var sorted = values.stream().sorted().toList();
        return sorted.get((int) Math.ceil(sorted.size() * quantile) - 1);
    }
}
