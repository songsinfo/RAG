package dev.rag;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

/**
 * 知识库系统 Spring Boot 应用程序主入口。
 * 启用定时任务支持 (@EnableScheduling)。
 */
@SpringBootApplication
@EnableScheduling
public class KnowledgeBaseApplication {
    /**
     * 应用程序主启动入口。
     *
     * @param args 命令行启动参数
     */
    public static void main(String[] args) {
        SpringApplication.run(KnowledgeBaseApplication.class, args);
    }
}

