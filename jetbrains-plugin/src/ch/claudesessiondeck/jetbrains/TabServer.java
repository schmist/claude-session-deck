package ch.claudesessiondeck.jetbrains;

import com.intellij.openapi.Disposable;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.application.ModalityState;
import com.intellij.openapi.diagnostic.Logger;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.project.ProjectManager;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowManager;
import com.intellij.openapi.wm.WindowManager;
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTab;
import com.intellij.terminal.frontend.toolwindow.TerminalToolWindowTabsManager;
import com.intellij.ui.content.Content;
import com.intellij.ui.content.ContentManager;
import kotlinx.coroutines.Deferred;
import org.jetbrains.plugins.terminal.session.TerminalStartupOptions;

import javax.swing.JFrame;
import java.io.BufferedReader;
import java.io.IOException;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.Channels;
import java.nio.channels.ClosedChannelException;
import java.nio.channels.ServerSocketChannel;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * Selects terminal tabs for the claude-session-deck daemon.
 *
 * Listens on $XDG_RUNTIME_DIR/claude-session-deck/jetbrains-PID.sock, PID being
 * this IDE's process. A request is one line, "focus PID PID ...", listing the
 * parent processes of a session. The reply is "ok", a tab and the title of the
 * window with the tab whose shell is one of them, or "none".
 */
public final class TabServer implements Disposable {
    private static final Logger LOG = Logger.getInstance(TabServer.class);

    private Path path;
    private ServerSocketChannel server;

    public TabServer() {
        String runtimeDir = System.getenv("XDG_RUNTIME_DIR");
        if (runtimeDir == null) {
            LOG.warn("XDG_RUNTIME_DIR is not set, so claude-session-deck cannot reach this IDE");
            return;
        }
        path = Path.of(runtimeDir, "claude-session-deck", "jetbrains-" + ProcessHandle.current().pid() + ".sock");
        try {
            Files.createDirectories(path.getParent());
            Files.deleteIfExists(path);  // left behind by a crashed IDE that had the same process ID
            server = ServerSocketChannel.open(StandardProtocolFamily.UNIX);
            server.bind(UnixDomainSocketAddress.of(path));
        } catch (IOException e) {
            LOG.warn("cannot listen on " + path, e);
            return;
        }
        Thread thread = new Thread(this::serve, "claude-session-deck");
        thread.setDaemon(true);
        thread.start();
    }

    private void serve() {
        while (true) {
            try (SocketChannel client = server.accept()) {
                String request = new BufferedReader(Channels.newReader(client, StandardCharsets.UTF_8)).readLine();
                String reply = request == null ? "none" : handle(request);
                client.write(ByteBuffer.wrap((reply + "\n").getBytes(StandardCharsets.UTF_8)));
            } catch (ClosedChannelException e) {
                return;  // disposed
            } catch (Throwable e) {  // also linkage errors, should the terminal's API change
                LOG.warn("request failed", e);
            }
        }
    }

    private String handle(String request) throws Exception {
        String[] words = request.trim().split("\\s+");
        if (!words[0].equals("focus")) {
            return "none";
        }
        Set<Long> pids = new HashSet<>();
        for (int i = 1; i < words.length; i++) {
            pids.add(Long.parseLong(words[i]));
        }
        CompletableFuture<String> title = new CompletableFuture<>();
        ApplicationManager.getApplication().invokeLater(() -> {
            try {
                title.complete(selectTab(pids));
            } catch (Throwable e) {
                title.completeExceptionally(e);
            }
        }, ModalityState.any());
        String found = title.get(2, TimeUnit.SECONDS);
        return found == null ? "none" : "ok\t" + found;
    }

    /** Selects the tab whose shell is one of the processes; returns its window's title, or null. */
    private static String selectTab(Set<Long> pids) {
        for (Project project : ProjectManager.getInstance().getOpenProjects()) {
            if (project.isDisposed()) {
                continue;
            }
            for (TerminalToolWindowTab tab : TerminalToolWindowTabsManager.getInstance(project).getTabs()) {
                Long pid = shellPid(tab);
                if (pid == null || !pids.contains(pid)) {
                    continue;
                }
                ToolWindow toolWindow = ToolWindowManager.getInstance(project).getToolWindow("Terminal");
                if (toolWindow != null) {
                    toolWindow.activate(null);
                }
                Content content = tab.getContent();
                ContentManager manager = content.getManager();
                if (manager != null) {
                    manager.setSelectedContent(content, true);
                }
                JFrame frame = WindowManager.getInstance().getFrame(project);
                return frame == null || frame.getTitle() == null ? "" : frame.getTitle();
            }
        }
        return null;
    }

    private static Long shellPid(TerminalToolWindowTab tab) {
        Deferred<TerminalStartupOptions> options = tab.getView().getStartupOptionsDeferred();
        if (!options.isCompleted() || options.isCancelled()) {
            return null;  // the shell has not started, or failed to
        }
        try {
            return options.getCompleted().getPid();
        } catch (RuntimeException e) {
            return null;
        }
    }

    @Override
    public void dispose() {
        if (server == null) {
            return;
        }
        try {
            server.close();
            Files.deleteIfExists(path);
        } catch (IOException e) {
            LOG.warn("cannot remove " + path, e);
        }
    }
}
