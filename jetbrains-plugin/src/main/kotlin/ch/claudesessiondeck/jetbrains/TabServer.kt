package ch.claudesessiondeck.jetbrains

import com.intellij.openapi.Disposable
import com.intellij.openapi.application.ApplicationManager
import com.intellij.openapi.application.ModalityState
import com.intellij.openapi.diagnostic.Logger
import com.intellij.openapi.project.ProjectManager
import com.intellij.openapi.wm.ToolWindowManager
import com.intellij.openapi.wm.WindowManager
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTab
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTabsManager
import kotlinx.coroutines.ExperimentalCoroutinesApi
import java.io.IOException
import java.net.StandardProtocolFamily
import java.net.UnixDomainSocketAddress
import java.nio.ByteBuffer
import java.nio.channels.Channels
import java.nio.channels.ClosedChannelException
import java.nio.channels.ServerSocketChannel
import java.nio.file.Files
import java.nio.file.Path
import java.util.concurrent.CompletableFuture
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

/**
 * Selects terminal tabs for the claude-session-deck daemon.
 *
 * Listens on $XDG_RUNTIME_DIR/claude-session-deck/jetbrains-PID.sock, PID being
 * this IDE's process. A request is one line, "focus PID PID ...", listing the
 * parent processes of a session. The reply is "ok", a tab and the title of the
 * window with the tab whose shell is one of them, or "none".
 */
class TabServer : Disposable {
    private val path: Path? = System.getenv("XDG_RUNTIME_DIR")?.let {
        Path.of(it, "claude-session-deck", "jetbrains-${ProcessHandle.current().pid()}.sock")
    }
    private val server: ServerSocketChannel? = path?.let(::listen)

    init {
        if (path == null) {
            LOG.warn("XDG_RUNTIME_DIR is not set, so claude-session-deck cannot reach this IDE")
        }
        server?.let { thread(isDaemon = true, name = "claude-session-deck") { serve(it) } }
    }

    private fun listen(path: Path): ServerSocketChannel? =
        try {
            Files.createDirectories(path.parent)
            Files.deleteIfExists(path)  // left behind by a crashed IDE that had the same process ID
            ServerSocketChannel.open(StandardProtocolFamily.UNIX).apply { bind(UnixDomainSocketAddress.of(path)) }
        } catch (e: IOException) {
            LOG.warn("cannot listen on $path", e)
            null
        }

    private fun serve(server: ServerSocketChannel) {
        while (true) {
            try {
                server.accept().use { client ->
                    val request = Channels.newReader(client, Charsets.UTF_8).buffered().readLine()
                    val reply = request?.let(::handle) ?: "none"
                    client.write(ByteBuffer.wrap("$reply\n".toByteArray()))
                }
            } catch (e: ClosedChannelException) {
                return  // disposed
            } catch (e: Throwable) {  // also linkage errors, should the terminal's API change
                LOG.warn("request failed", e)
            }
        }
    }

    /** The reply to a request; null if it asks for nothing this server knows, or no tab is found. */
    private fun handle(request: String): String? {
        val words = request.trim().split(Regex("\\s+"))
        if (words[0] != "focus") {
            return null
        }
        val pids = words.drop(1).map(String::toLong).toSet()
        val title = CompletableFuture<String?>()
        ApplicationManager.getApplication().invokeLater({
            try {
                title.complete(selectTab(pids))
            } catch (e: Throwable) {
                title.completeExceptionally(e)
            }
        }, ModalityState.any())
        return title.get(2, TimeUnit.SECONDS)?.let { "ok\t$it" }
    }

    /** Selects the tab whose shell is one of the processes; returns its window's title, or null. */
    private fun selectTab(pids: Set<Long>): String? {
        for (project in ProjectManager.getInstance().openProjects.filterNot { it.isDisposed }) {
            val tab = TerminalToolWindowTabsManager.getInstance(project).tabs.find { shellPid(it) in pids } ?: continue
            ToolWindowManager.getInstance(project).getToolWindow("Terminal")?.activate(null)
            tab.content.manager?.setSelectedContent(tab.content, true)
            return WindowManager.getInstance().getFrame(project)?.title ?: ""
        }
        return null
    }

    @OptIn(ExperimentalCoroutinesApi::class)
    private fun shellPid(tab: TerminalToolWindowTab): Long? {
        val options = tab.view.startupOptionsDeferred
        if (!options.isCompleted || options.isCancelled) {
            return null  // the shell has not started, or failed to
        }
        return try {
            options.getCompleted().pid
        } catch (e: RuntimeException) {
            null
        }
    }

    override fun dispose() {
        try {
            server?.close()
            path?.let(Files::deleteIfExists)
        } catch (e: IOException) {
            LOG.warn("cannot remove $path", e)
        }
    }

    private companion object {
        val LOG = Logger.getInstance(TabServer::class.java)
    }
}
