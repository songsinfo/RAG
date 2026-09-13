package dev.rag.common;

import org.flywaydb.core.Flyway;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

import javax.sql.DataSource;

/**
 * 数据库 Flyway 自动迁移配置类。
 * 负责在应用启动时触发数据库模式脚本执行，确保数据表结构与版本同步。
 */
@Configuration
public class DatabaseMigrationConfig {

    /**
     * 初始化并配置 Flyway 迁移实例。
     * 指定启动时执行 migrate 动作，支持自定义脚本路径配置。
     *
     * @param dataSource 应用程序配置的数据源
     * @param locations  Flyway 迁移脚本所在的资源目录位置，默认为 classpath:db/migration
     * @return 配置就绪的 Flyway 实例
     */
    @Bean(initMethod = "migrate")
    public Flyway flyway(DataSource dataSource, @Value("${spring.flyway.locations:classpath:db/migration}") String locations) {
        return Flyway.configure()
                .dataSource(dataSource)
                .locations(locations)
                .load();
    }
}
