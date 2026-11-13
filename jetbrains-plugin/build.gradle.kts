// Builds the plugin against an installed IDE, so that none is downloaded:
//   ./gradlew installPlugin -Pide=<IDE home> -Pplugins=<plugins directory>
// The result is <plugins directory>/claude-session-deck/. Run it with the Java
// the IDE ships (JAVA_HOME=<IDE home>/jbr): the classes are made for that one.
plugins {
    kotlin("jvm") version "2.4.21"
    id("org.jetbrains.intellij.platform") version "2.20.1"
}

version = "1"

repositories {
    mavenCentral()
    intellijPlatform {
        defaultRepositories()
    }
}

dependencies {
    intellijPlatform {
        local(providers.gradleProperty("ide"))
        bundledPlugin("org.jetbrains.plugins.terminal")
    }
}

intellijPlatform {
    buildSearchableOptions = false  // the plugin has no settings
    instrumentCode = false          // nor forms or nullability annotations of Java
}

tasks.register<Sync>("installPlugin") {
    description = "Installs the plugin into the plugins directory given as -Pplugins."
    group = "intellij platform"
    from(zipTree(tasks.buildPlugin.flatMap { it.archiveFile })) {
        eachFile { path = path.substringAfter("/") }  // without the folder the archive holds
        includeEmptyDirs = false
    }
    into(providers.gradleProperty("plugins").map { "$it/${rootProject.name}" })
}
