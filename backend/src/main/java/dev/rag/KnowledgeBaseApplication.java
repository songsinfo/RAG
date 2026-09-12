package dev.rag;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;
@SpringBootApplication
@EnableScheduling
public class KnowledgeBaseApplication {
 public static void main(String[] args) { SpringApplication.run(KnowledgeBaseApplication.class,args); }
}

