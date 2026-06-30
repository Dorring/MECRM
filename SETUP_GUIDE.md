# Enterprise CRM - Complete Setup Guide

## Prerequisites - What You Need to Install

### 1. 🐳 Docker Desktop (Required)
**Purpose**: Runs all infrastructure services (PostgreSQL, Redis, Kafka, etc.)

**Download**: https://www.docker.com/products/docker-desktop/

**Installation Steps**:
1. Download Docker Desktop for Windows
2. Run the installer
3. Restart your computer
4. Open Docker Desktop and wait for it to start
5. Verify installation:
   ```powershell
   docker --version
   docker-compose --version
   ```

---

### 2. 📗 Node.js 20+ (Required for Gateway & Frontend)
**Purpose**: Runs the API Gateway and Frontend

**Download**: https://nodejs.org/en/download/ (Choose LTS version)

**Installation Steps**:
1. Download the Windows installer (.msi)
2. Run installer, accept all defaults
3. Verify installation:
   ```powershell
   node --version   # Should show v20.x.x or higher
   npm --version    # Should show 10.x.x
   ```

---

### 3. 🐍 Python 3.11+ (Required for AI Agents)
**Purpose**: Runs the AI Agent orchestrator

**Download**: https://www.python.org/downloads/

**Installation Steps**:
1. Download Python 3.11 or 3.12 installer
2. **IMPORTANT**: Check "Add Python to PATH" during installation
3. Verify installation:
   ```powershell
   python --version  # Should show 3.11.x or higher
   pip --version
   ```

---

### 4. 📦 Git (Recommended)
**Purpose**: Version control

**Download**: https://git-scm.com/download/win

---

## Project Setup Steps

### Step 1: Start Docker Desktop
Make sure Docker Desktop is running (whale icon in system tray).

### Step 2: Configure Environment
```powershell
cd F:\Dev_Env\Ai-Test-Engineer-Agent

# Copy example env (already done for you)
Copy-Item .env.example .env
```

### Step 3: Start Infrastructure Services
```powershell
# Start databases and messaging
docker-compose up -d postgres redis kafka opa kafka-ui prometheus grafana
```

Wait 1-2 minutes for all services to be healthy.

### Step 4: Verify Services Running
```powershell
docker ps
```

You should see these containers running:
| Container | Port | Purpose |
|-----------|------|---------|
| postgres | 5432 | Database |
| redis | 6379 | Cache |
| kafka | 9092, 9094 | Messaging |
| opa | 8181 | Policies |
| prometheus | 9090 | Metrics |
| grafana | 3001 | Dashboards |

### Step 5: Setup Gateway (API)
```powershell
cd gateway
npm install
npx prisma generate
npx prisma migrate dev --name init
```

### Step 6: Setup Frontend
```powershell
cd ..\frontend
npm install
```

### Step 7: Setup AI Agents (Optional)
```powershell
cd ..\agents
pip install -r requirements.txt
```

---

## Running the Application

### Terminal 1: Start Gateway (API)
```powershell
cd F:\Dev_Env\Ai-Test-Engineer-Agent\gateway
npm run dev
```
Gateway runs at: http://localhost:4000

### Terminal 2: Start Frontend
```powershell
cd F:\Dev_Env\Ai-Test-Engineer-Agent\frontend
npm run dev
```
Frontend runs at: http://localhost:3000

### Terminal 3: Start Agents (Optional)
```powershell
cd F:\Dev_Env\Ai-Test-Engineer-Agent\agents
python -m src.orchestrator.main
```
Agents run at: http://localhost:5010

---

## Access Points

| Service | URL | Credentials |
|---------|-----|-------------|
| Frontend | http://localhost:3000 | Register new account |
| API Gateway | http://localhost:4000 | - |
| API Health | http://localhost:4000/health | - |
| Kafka UI | http://localhost:8080 | - |
| Grafana | http://localhost:3001 | admin / admin |
| Prometheus | http://localhost:9090 | - |
| Keycloak | http://localhost:8081 | admin / admin |

---

## Troubleshooting

### Docker Issues
```powershell
# Restart Docker services
docker-compose down
docker-compose up -d postgres redis kafka opa

# View logs
docker-compose logs postgres
docker-compose logs kafka
```

### Database Connection Issues
```powershell
# Test database connection
docker exec -it ai-test-engineer-agent-postgres-1 psql -U crm_user -d enterprise_crm
```

### Port Already in Use
```powershell
# Find process using port
netstat -ano | findstr :5432

# Kill process (replace PID)
taskkill /PID <PID> /F
```

### Reset Everything
```powershell
docker-compose down -v  # Removes volumes too
docker-compose up -d postgres redis kafka opa
```

---

## Minimum System Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| RAM | 8 GB | 16 GB |
| CPU | 4 cores | 8 cores |
| Disk | 20 GB free | 50 GB free |
| OS | Windows 10/11 | Windows 11 |

---

## Optional Tools

### VS Code Extensions
- Docker
- Prisma
- ESLint
- Prettier
- Python
- Thunder Client (API testing)

### Database GUI
- **pgAdmin**: https://www.pgadmin.org/download/
- **DBeaver**: https://dbeaver.io/download/

### API Testing
- **Postman**: https://www.postman.com/downloads/
- **Insomnia**: https://insomnia.rest/download
